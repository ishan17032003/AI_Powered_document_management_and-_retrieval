"""Transaction-neutral USER-to-GROUP membership persistence.

Membership changes are authorization changes.  This repository therefore
validates every foreign identity, performs an idempotent upsert/delete, and
bumps the singleton authorization-policy revision in the same caller-owned
transaction.  It deliberately never commits or rolls back the supplied
session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .. import models
from .policy_revision_repository import PolicyRevisionError, bump as bump_policy_revision, current as current_policy_revision

_MAX_IDENTIFIER = 2_147_483_647
_HARD_MAX_MEMBERS = 4_096


class GroupMembershipError(RuntimeError):
    """Stable, fail-closed membership mutation failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MembershipMutation:
    """Result of one membership mutation.

    ``changed`` is false for an idempotent duplicate add or missing remove.
    The policy revision is returned for both paths so callers can associate a
    decision with the exact current authorization state.
    """

    group_id: int
    user_id: int
    changed: bool
    membership_id: int | None
    policy_revision: int


def _require_identifier(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0 or value > _MAX_IDENTIFIER:
        raise ValueError(f"{field} must be a positive bounded integer")
    return value


def _require_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _HARD_MAX_MEMBERS:
        raise ValueError(f"limit must be between 1 and {_HARD_MAX_MEMBERS}")
    return value


def _require_user(db: Session, user_id: int, *, field: str) -> None:
    if (
        db.execute(
            select(models.User.id).where(models.User.id == user_id)
        ).scalar_one_or_none()
        is None
    ):
        raise GroupMembershipError(f"AUTHZ_{field.upper()}_NOT_FOUND")


def _require_active_group(db: Session, group_id: int) -> None:
    row = db.execute(
        select(models.Group.id, models.Group.is_active).where(
            models.Group.id == group_id
        )
    ).one_or_none()
    if row is None:
        raise GroupMembershipError("AUTHZ_GROUP_NOT_FOUND")
    if type(row.is_active) is not bool or not row.is_active:
        raise GroupMembershipError("AUTHZ_GROUP_INACTIVE")


def _load_policy_revision(db: Session) -> int:
    try:
        return current_policy_revision(db)
    except PolicyRevisionError as exc:
        raise GroupMembershipError(str(exc)) from exc


def _increment_policy_revision(
    db: Session,
    *,
    actor_id: int,
) -> int:
    """Increment policy state atomically and return the new revision."""

    try:
        return bump_policy_revision(db, actor_id=actor_id)
    except PolicyRevisionError as exc:
        raise GroupMembershipError(str(exc)) from exc


def _insert_idempotent(
    db: Session,
    *,
    group_id: int,
    user_id: int,
    created_by: int,
) -> bool:
    values = {
        "group_id": group_id,
        "user_id": user_id,
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc),
    }
    dialect = db.get_bind().dialect.name
    statement: Any
    if dialect == "sqlite":
        statement = sqlite_insert(models.GroupMembership).values(**values)
    elif dialect == "postgresql":
        statement = postgresql_insert(models.GroupMembership).values(**values)
    else:
        raise GroupMembershipError("AUTHZ_MEMBERSHIP_DATABASE_UNSUPPORTED")
    result = db.execute(
        statement.on_conflict_do_nothing(
            index_elements=("group_id", "user_id"),
        )
    )
    return getattr(result, "rowcount", None) == 1


def add_membership(
    db: Session,
    *,
    group_id: int,
    user_id: int,
    created_by: int,
) -> MembershipMutation:
    """Idempotently add a USER membership without ending the caller transaction."""

    group_id = _require_identifier(group_id, field="group_id")
    user_id = _require_identifier(user_id, field="user_id")
    created_by = _require_identifier(created_by, field="created_by")
    _require_active_group(db, group_id)
    _require_user(db, user_id, field="user")
    _require_user(db, created_by, field="creator")
    changed = _insert_idempotent(
        db,
        group_id=group_id,
        user_id=user_id,
        created_by=created_by,
    )
    if changed:
        revision = _increment_policy_revision(
            db,
            actor_id=created_by,
        )
    else:
        # A concurrent writer may have committed between the preflight read
        # and the conflict-safe insert.  Never return a stale revision token.
        revision = _load_policy_revision(db)
    membership_id = db.execute(
        select(models.GroupMembership.id).where(
            models.GroupMembership.group_id == group_id,
            models.GroupMembership.user_id == user_id,
        )
    ).scalar_one_or_none()
    if changed and membership_id is None:
        raise GroupMembershipError("AUTHZ_MEMBERSHIP_WRITE_UNAVAILABLE")
    return MembershipMutation(
        group_id=group_id,
        user_id=user_id,
        changed=changed,
        membership_id=membership_id,
        policy_revision=revision,
    )


def remove_membership(
    db: Session,
    *,
    group_id: int,
    user_id: int,
    removed_by: int,
) -> MembershipMutation:
    """Idempotently remove a USER membership without committing the session."""

    group_id = _require_identifier(group_id, field="group_id")
    user_id = _require_identifier(user_id, field="user_id")
    removed_by = _require_identifier(removed_by, field="removed_by")
    _require_active_group(db, group_id)
    _require_user(db, user_id, field="user")
    _require_user(db, removed_by, field="remover")
    current_revision = _load_policy_revision(db)

    membership_id = db.execute(
        select(models.GroupMembership.id).where(
            models.GroupMembership.group_id == group_id,
            models.GroupMembership.user_id == user_id,
        )
    ).scalar_one_or_none()
    if membership_id is None:
        return MembershipMutation(
            group_id=group_id,
            user_id=user_id,
            changed=False,
            membership_id=None,
            policy_revision=current_revision,
        )

    result = db.execute(
        delete(models.GroupMembership).where(
            models.GroupMembership.group_id == group_id,
            models.GroupMembership.user_id == user_id,
        )
    )
    if getattr(result, "rowcount", None) != 1:
        raise GroupMembershipError("AUTHZ_MEMBERSHIP_WRITE_UNAVAILABLE")
    revision = _increment_policy_revision(
        db,
        actor_id=removed_by,
    )
    return MembershipMutation(
        group_id=group_id,
        user_id=user_id,
        changed=True,
        membership_id=membership_id,
        policy_revision=revision,
    )


def list_member_ids(
    db: Session,
    *,
    group_id: int,
    limit: int = 256,
) -> tuple[int, ...]:
    """Return a bounded, stable member list; oversized groups fail closed."""

    group_id = _require_identifier(group_id, field="group_id")
    limit = _require_limit(limit)
    _require_active_group(db, group_id)
    rows = db.execute(
        select(models.GroupMembership.user_id)
        .where(models.GroupMembership.group_id == group_id)
        .order_by(models.GroupMembership.user_id)
        .limit(limit + 1)
    ).all()
    if len(rows) > limit:
        raise GroupMembershipError("AUTHZ_MEMBER_LIMIT")
    try:
        return tuple(_require_identifier(row[0], field="user_id") for row in rows)
    except (TypeError, ValueError):
        raise GroupMembershipError("AUTHZ_MEMBER_DATA_INVALID") from None


def add_user_to_group(
    db: Session,
    *,
    group_id: int,
    user_id: int,
    actor_id: int,
) -> MembershipMutation:
    """Explicit USER-principal spelling for repository callers."""

    return add_membership(
        db,
        group_id=group_id,
        user_id=user_id,
        created_by=actor_id,
    )


def remove_user_from_group(
    db: Session,
    *,
    group_id: int,
    user_id: int,
    actor_id: int,
) -> MembershipMutation:
    """Explicit USER-principal spelling for repository callers."""

    return remove_membership(
        db,
        group_id=group_id,
        user_id=user_id,
        removed_by=actor_id,
    )
