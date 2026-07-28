"""Focused transaction and cache-invalidation tests for USER memberships."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session


def _group(db: Session, creator_id: int, *, active: bool = True):
    from app import models

    group = models.Group(
        name=f"membership-{uuid4().hex}",
        description="membership service test group",
        is_active=active,
        created_by=creator_id,
    )
    db.add(group)
    db.flush()
    return group


def test_add_remove_resolves_principal_and_bumps_revision(
    db_session: Session,
    user_factory,
) -> None:
    from app import models
    from app.repositories import access_rule_repository
    from app.services.group_membership_service import GroupMembershipService

    actor = user_factory()
    member = user_factory()
    group = _group(db_session, actor.id)
    policy = db_session.get(models.AuthorizationPolicyState, 1)
    assert policy is not None
    assert policy.revision == 0

    added = GroupMembershipService.add_user_to_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    assert added.changed is True
    assert added.membership_id is not None
    assert added.policy_revision == 1
    assert db_session.in_transaction()
    assert access_rule_repository.load_principal_set(db_session, member.id).groups
    assert access_rule_repository.load_policy_revision(db_session) == 1

    removed = GroupMembershipService.remove_user_from_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    assert removed.changed is True
    assert removed.policy_revision == 2
    assert not access_rule_repository.load_principal_set(db_session, member.id).groups
    assert access_rule_repository.load_policy_revision(db_session) == 2


def test_duplicate_add_and_missing_remove_are_idempotent_without_revision_bump(
    db_session: Session,
    user_factory,
) -> None:
    from app import models
    from app.services.group_membership_service import GroupMembershipService

    actor = user_factory()
    member = user_factory()
    group = _group(db_session, actor.id)

    first = GroupMembershipService.add_user_to_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    duplicate = GroupMembershipService.add_user_to_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    assert first.policy_revision == duplicate.policy_revision == 1
    assert duplicate.changed is False
    assert duplicate.membership_id == first.membership_id
    assert db_session.query(models.GroupMembership).count() == 1

    removed = GroupMembershipService.remove_user_from_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    missing = GroupMembershipService.remove_user_from_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    assert removed.policy_revision == missing.policy_revision == 2
    assert missing.changed is False
    assert missing.membership_id is None


def test_invalid_ids_users_and_inactive_groups_fail_closed(
    db_session: Session,
    user_factory,
) -> None:
    from app.services.group_membership_service import (
        GroupMembershipError,
        GroupMembershipService,
    )

    actor = user_factory()
    member = user_factory()
    inactive = _group(db_session, actor.id, active=False)

    with pytest.raises(ValueError):
        GroupMembershipService.add_user_to_group(
            db_session,
            group_id=0,
            user_id=member.id,
            actor_id=actor.id,
        )
    with pytest.raises(GroupMembershipError, match="AUTHZ_GROUP_INACTIVE"):
        GroupMembershipService.add_user_to_group(
            db_session,
            group_id=inactive.id,
            user_id=member.id,
            actor_id=actor.id,
        )
    active = _group(db_session, actor.id)
    with pytest.raises(GroupMembershipError, match="AUTHZ_USER_NOT_FOUND"):
        GroupMembershipService.add_user_to_group(
            db_session,
            group_id=active.id,
            user_id=member.id + 9999,
            actor_id=actor.id,
        )
    with pytest.raises(GroupMembershipError, match="AUTHZ_CREATOR_NOT_FOUND"):
        GroupMembershipService.add_user_to_group(
            db_session,
            group_id=active.id,
            user_id=member.id,
            actor_id=actor.id + 9999,
        )


def test_member_list_is_bounded_and_stable(
    db_session: Session,
    user_factory,
) -> None:
    from app.services.group_membership_service import GroupMembershipService

    actor = user_factory()
    member = user_factory()
    group = _group(db_session, actor.id)
    GroupMembershipService.add_user_to_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    assert GroupMembershipService.list_member_ids(
        db_session,
        group_id=group.id,
        limit=1,
    ) == (member.id,)
    with pytest.raises(ValueError):
        GroupMembershipService.list_member_ids(
            db_session,
            group_id=group.id,
            limit=0,
        )


def test_duplicate_from_second_session_is_safe_and_rollback_is_atomic(
    db_session: Session,
    user_factory,
) -> None:
    from app import database
    from app.repositories import access_rule_repository
    from app.services.group_membership_service import GroupMembershipService

    actor = user_factory()
    member = user_factory()
    group = _group(db_session, actor.id)
    db_session.commit()

    first = GroupMembershipService.add_user_to_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    db_session.commit()
    second_session = database.SessionLocal()
    try:
        duplicate = GroupMembershipService.add_user_to_group(
            second_session,
            group_id=group.id,
            user_id=member.id,
            actor_id=actor.id,
        )
        assert first.changed is True
        assert duplicate.changed is False
        assert duplicate.policy_revision == first.policy_revision == 1
    finally:
        second_session.rollback()
        second_session.close()

    # The row and revision are one caller-owned transaction: rollback removes
    # both, so a failed/abandoned caller cannot leave authorization half-changed.
    second_group = _group(db_session, actor.id)
    db_session.commit()
    GroupMembershipService.add_user_to_group(
        db_session,
        group_id=second_group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    db_session.rollback()
    principals = access_rule_repository.load_principal_set(db_session, member.id)
    assert len(principals.groups) == 1
    assert next(iter(principals.groups)).principal_id == group.id

    # A remove rolled back likewise leaves the previously committed membership.
    removed = GroupMembershipService.remove_user_from_group(
        db_session,
        group_id=group.id,
        user_id=member.id,
        actor_id=actor.id,
    )
    assert removed.changed is True
    db_session.rollback()
    assert access_rule_repository.load_principal_set(db_session, member.id).groups
    assert access_rule_repository.load_policy_revision(db_session) == 1


def test_concurrent_duplicate_adds_commit_one_membership_and_one_revision(
    db_session: Session,
    user_factory,
) -> None:
    from app import database, models
    from app.services.group_membership_service import GroupMembershipService

    actor = user_factory()
    member = user_factory()
    group = _group(db_session, actor.id)
    db_session.commit()
    barrier = Barrier(2)

    def worker():
        session = database.SessionLocal()
        try:
            barrier.wait(timeout=10)
            result = GroupMembershipService.add_user_to_group(
                session,
                group_id=group.id,
                user_id=member.id,
                actor_id=actor.id,
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: worker(), (1, 2)))

    assert sorted(result.changed for result in results) == [False, True]
    assert db_session.query(models.GroupMembership).filter_by(
        group_id=group.id,
        user_id=member.id,
    ).count() == 1
    policy = db_session.get(models.AuthorizationPolicyState, 1)
    assert policy is not None
    assert policy.revision == 1
