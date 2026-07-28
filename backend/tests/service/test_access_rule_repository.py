"""Bounded persistence tests for one authoritative ACL decision input load."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app import models


def _permission(db: Session, code: str) -> models.Permission:
    from app import models

    permission = (
        db.query(models.Permission).filter(models.Permission.code == code).one_or_none()
    )
    if permission is None:
        permission = models.Permission(code=code)
        db.add(permission)
        db.flush()
    return permission


def _grant_capabilities(
    db: Session,
    user: models.User,
    codes: set[str],
    *,
    effect: str = "ALLOW",
) -> None:
    from app import models

    role = models.Role(name=f"capability-bundle-{uuid4().hex}")
    db.add(role)
    db.flush()
    for code in sorted(codes):
        permission = _permission(db, code)
        db.add(
            models.RolePermission(
                role_id=role.id,
                permission_id=permission.id,
            )
        )
    db.add(
        models.Assignment(
            user_id=user.id,
            role_id=role.id,
            scope_type="GLOBAL",
            scope_id=None,
            effect=effect,
        )
    )
    db.flush()


def _group(
    db: Session,
    *,
    creator: models.User,
    member: models.User,
    active: bool = True,
) -> models.Group:
    from app import models

    group = models.Group(
        name=f"group-{uuid4().hex}",
        description="ACL repository test group",
        is_active=active,
        created_by=creator.id,
    )
    db.add(group)
    db.flush()
    db.add(
        models.GroupMembership(
            group_id=group.id,
            user_id=member.id,
            created_by=creator.id,
        )
    )
    db.flush()
    return group


def _hierarchy(
    db: Session,
    *,
    creator: models.User,
    cabinet_depth: int,
    folder_depth: int,
) -> tuple[list[models.Cabinet], list[models.Folder], models.Document]:
    from app import models

    cabinets: list[models.Cabinet] = []
    parent_cabinet_id: int | None = None
    for index in range(cabinet_depth):
        cabinet = models.Cabinet(
            name=f"cabinet-{index}-{uuid4().hex}",
            parent_id=parent_cabinet_id,
        )
        db.add(cabinet)
        db.flush()
        cabinets.append(cabinet)
        parent_cabinet_id = cabinet.id

    folders: list[models.Folder] = []
    parent_folder_id: int | None = None
    for index in range(folder_depth):
        folder = models.Folder(
            cabinet_id=cabinets[-1].id,
            parent_id=parent_folder_id,
            name=f"folder-{index}-{uuid4().hex}",
        )
        db.add(folder)
        db.flush()
        folders.append(folder)
        parent_folder_id = folder.id

    document = models.Document(
        folder_id=folders[-1].id,
        title=f"document-{uuid4().hex}",
        content_hash=uuid4().hex * 2,
        created_by=creator.id,
    )
    db.add(document)
    db.flush()
    return cabinets, folders, document


def _access_rule(
    db: Session,
    *,
    creator: models.User,
    permission: models.Permission,
    principal: models.User | models.Group,
    scope_type: str,
    scope_id: int | None,
    effect: str = "ALLOW",
    inherits: bool = True,
    active: bool = True,
    expires_at: datetime | None = None,
) -> models.AccessRule:
    from app import models

    is_user = isinstance(principal, models.User)
    rule = models.AccessRule(
        principal_type="USER" if is_user else "GROUP",
        user_id=principal.id if is_user else None,
        group_id=None if is_user else principal.id,
        permission_id=permission.id,
        scope_type=scope_type,
        scope_id=scope_id,
        effect=effect,
        inherits=inherits,
        is_active=active,
        expires_at=expires_at,
        reason="Repository test rule",
        created_by=creator.id,
    )
    db.add(rule)
    db.flush()
    return rule


@contextmanager
def _capture_statements(db: Session) -> Iterator[list[str]]:
    statements: list[str] = []
    bind = db.get_bind()

    def capture(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(bind, "before_cursor_execute", capture)


def test_complete_decision_input_load_converts_and_filters_rows(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    from app import models
    from app.domain import (
        AccessEffect,
        HardPolicyGates,
        PrincipalRef,
        ResourceRef,
    )
    from app.repositories import access_rule_repository
    from app.services.authorization_service import evaluate_authorization

    user = user_factory()
    other_user = user_factory()
    _grant_capabilities(db_session, user, {"VIEW", "DOWNLOAD"})
    _grant_capabilities(db_session, user, {"DOWNLOAD"}, effect="DENY")
    view_permission = _permission(db_session, "VIEW")
    edit_permission = _permission(db_session, "EDIT_CONTENT")
    active_group = _group(
        db_session,
        creator=user,
        member=user,
    )
    inactive_group = _group(
        db_session,
        creator=user,
        member=user,
        active=False,
    )
    cabinets, folders, document = _hierarchy(
        db_session,
        creator=user,
        cabinet_depth=2,
        folder_depth=2,
    )
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    global_rule = _access_rule(
        db_session,
        creator=user,
        permission=view_permission,
        principal=user,
        scope_type="GLOBAL",
        scope_id=None,
    )
    document_rule = _access_rule(
        db_session,
        creator=user,
        permission=view_permission,
        principal=active_group,
        scope_type="DOC",
        scope_id=document.id,
        expires_at=datetime(2026, 7, 27, 8, 0),
    )
    _access_rule(
        db_session,
        creator=user,
        permission=view_permission,
        principal=user,
        scope_type="FOLDER",
        scope_id=folders[-1].id,
        active=False,
    )
    _access_rule(
        db_session,
        creator=user,
        permission=view_permission,
        principal=user,
        scope_type="CABINET",
        scope_id=cabinets[-1].id,
        expires_at=datetime(2026, 7, 27, 6, 29, 59),
    )
    _access_rule(
        db_session,
        creator=user,
        permission=view_permission,
        principal=user,
        scope_type="CABINET",
        scope_id=cabinets[0].id,
        expires_at=datetime(2026, 7, 27, 6, 30),
    )
    _access_rule(
        db_session,
        creator=user,
        permission=view_permission,
        principal=inactive_group,
        scope_type="DOC",
        scope_id=document.id,
    )
    _access_rule(
        db_session,
        creator=user,
        permission=view_permission,
        principal=other_user,
        scope_type="DOC",
        scope_id=document.id,
    )
    _access_rule(
        db_session,
        creator=user,
        permission=edit_permission,
        principal=user,
        scope_type="DOC",
        scope_id=document.id,
    )
    policy = db_session.get(models.AuthorizationPolicyState, 1)
    assert policy is not None
    policy.revision = 17
    db_session.flush()

    inputs = access_rule_repository.load_authorization_decision_inputs(
        db_session,
        user_id=user.id,
        permission="VIEW",
        resource=ResourceRef.document(document.id),
        now=now,
    )

    assert inputs.effective_permissions == frozenset({"VIEW"})
    assert inputs.principals.user == PrincipalRef.user(user.id)
    assert inputs.principals.groups == frozenset({PrincipalRef.group(active_group.id)})
    assert inputs.ancestry.resources == (
        ResourceRef.global_scope(),
        ResourceRef.cabinet(cabinets[0].id),
        ResourceRef.cabinet(cabinets[1].id),
        ResourceRef.folder(folders[0].id),
        ResourceRef.folder(folders[1].id),
        ResourceRef.document(document.id),
    )
    assert tuple(rule.rule_id for rule in inputs.rules) == (
        global_rule.id,
        document_rule.id,
    )
    assert inputs.rules[1].effect is AccessEffect.ALLOW
    assert inputs.rules[1].expires_at == datetime(
        2026,
        7,
        27,
        8,
        0,
        tzinfo=timezone.utc,
    )
    assert inputs.policy_version == 17
    assert inputs.evaluated_at == datetime(
        2026,
        7,
        27,
        6,
        30,
        tzinfo=timezone.utc,
    )
    decision = evaluate_authorization(
        gates=HardPolicyGates(
            authenticated=True,
            account_active=True,
            security_boundary_allowed=True,
            hard_policy_allowed=True,
        ),
        effective_permissions=inputs.effective_permissions,
        principals=inputs.principals,
        permission="VIEW",
        ancestry=inputs.ancestry,
        rules=inputs.rules,
        policy_version=inputs.policy_version,
        now=inputs.evaluated_at,
    )
    assert decision.allowed
    assert decision.matched_rule_id == document_rule.id


def test_one_document_decision_remains_exactly_five_statements_at_scale(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    from app.domain import ResourceRef
    from app.repositories import access_rule_repository

    view_permission = _permission(db_session, "VIEW")
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)

    def build_case(
        *,
        group_count: int,
        cabinet_depth: int,
        folder_depth: int,
    ) -> tuple[models.User, models.Document]:
        user = user_factory()
        _grant_capabilities(db_session, user, {"VIEW"})
        _, _, document = _hierarchy(
            db_session,
            creator=user,
            cabinet_depth=cabinet_depth,
            folder_depth=folder_depth,
        )
        for _ in range(group_count):
            group = _group(db_session, creator=user, member=user)
            _access_rule(
                db_session,
                creator=user,
                permission=view_permission,
                principal=group,
                scope_type="DOC",
                scope_id=document.id,
            )
        return user, document

    shallow_user, shallow_document = build_case(
        group_count=1,
        cabinet_depth=1,
        folder_depth=1,
    )
    deep_user, deep_document = build_case(
        group_count=24,
        cabinet_depth=7,
        folder_depth=9,
    )

    counts: list[int] = []
    for user, document in (
        (shallow_user, shallow_document),
        (deep_user, deep_document),
    ):
        with _capture_statements(db_session) as statements:
            inputs = access_rule_repository.load_authorization_decision_inputs(
                db_session,
                user_id=user.id,
                permission="VIEW",
                resource=ResourceRef.document(document.id),
                now=now,
            )
        counts.append(len(statements))
        assert len(inputs.principals.groups) in {1, 24}
        assert len(inputs.rules) in {1, 24}
        assert all(
            statement.lstrip().upper().startswith(("SELECT", "WITH"))
            for statement in statements
        )

    assert counts == [5, 5]


def test_missing_cyclic_and_over_depth_hierarchies_fail_closed(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    from app import models
    from app.domain import ResourceRef
    from app.repositories import access_rule_repository

    user = user_factory()
    with pytest.raises(
        access_rule_repository.AuthorizationInputUnavailable,
        match="AUTHZ_ANCESTRY_MISSING",
    ):
        access_rule_repository.resolve_resource_ancestry(
            db_session,
            ResourceRef.document(999_999),
        )

    first = models.Cabinet(name="cycle-first", parent_id=None)
    db_session.add(first)
    db_session.flush()
    second = models.Cabinet(name="cycle-second", parent_id=first.id)
    db_session.add(second)
    db_session.flush()
    first.parent_id = second.id
    db_session.flush()

    with pytest.raises(
        access_rule_repository.AuthorizationInputUnavailable,
        match="AUTHZ_ANCESTRY_INVALID",
    ):
        access_rule_repository.resolve_resource_ancestry(
            db_session,
            ResourceRef.cabinet(first.id),
        )

    cabinets, _, _ = _hierarchy(
        db_session,
        creator=user,
        cabinet_depth=4,
        folder_depth=1,
    )
    limits = access_rule_repository.AuthorizationRepositoryLimits(
        max_ancestry_resources=3
    )
    with pytest.raises(
        access_rule_repository.AuthorizationInputUnavailable,
        match="AUTHZ_ANCESTRY_LIMIT",
    ):
        access_rule_repository.resolve_resource_ancestry(
            db_session,
            ResourceRef.cabinet(cabinets[-1].id),
            limits=limits,
        )


def test_permission_group_and_rule_cardinality_limits_fail_closed(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    from app.domain import PrincipalSet, ResourceAncestry, ResourceRef
    from app.repositories import access_rule_repository

    user = user_factory()
    _grant_capabilities(db_session, user, {"VIEW", "DOWNLOAD", "EDIT_CONTENT"})
    groups = [_group(db_session, creator=user, member=user) for _ in range(3)]
    permission = _permission(db_session, "VIEW")
    for group in groups:
        _access_rule(
            db_session,
            creator=user,
            permission=permission,
            principal=group,
            scope_type="GLOBAL",
            scope_id=None,
        )

    permission_limits = access_rule_repository.AuthorizationRepositoryLimits(
        max_effective_permissions=2
    )
    with pytest.raises(
        access_rule_repository.AuthorizationInputUnavailable,
        match="AUTHZ_PERMISSION_LIMIT",
    ):
        access_rule_repository.load_effective_permission_codes(
            db_session,
            user.id,
            limits=permission_limits,
        )

    group_limits = access_rule_repository.AuthorizationRepositoryLimits(max_groups=2)
    with pytest.raises(
        access_rule_repository.AuthorizationInputUnavailable,
        match="AUTHZ_GROUP_LIMIT",
    ):
        access_rule_repository.load_principal_set(
            db_session,
            user.id,
            limits=group_limits,
        )

    rule_limits = access_rule_repository.AuthorizationRepositoryLimits(max_rules=2)
    with pytest.raises(
        access_rule_repository.AuthorizationInputUnavailable,
        match="AUTHZ_RULE_LIMIT",
    ):
        access_rule_repository.load_access_rules(
            db_session,
            permission="VIEW",
            principals=PrincipalSet.from_ids(
                user.id,
                (group.id for group in groups),
            ),
            ancestry=ResourceAncestry((ResourceRef.global_scope(),)),
            now=datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
            limits=rule_limits,
        )


def test_malformed_role_state_and_missing_policy_revision_fail_closed(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    from app import models
    from app.repositories import access_rule_repository

    user = user_factory()
    # MIG-004 now rejects malformed assignment state at the database boundary.
    with pytest.raises(IntegrityError):
        _grant_capabilities(db_session, user, {"VIEW"}, effect="MAYBE")
    db_session.rollback()
    user = user_factory()
    _grant_capabilities(db_session, user, {"VIEW"})

    policy = db_session.get(models.AuthorizationPolicyState, 1)
    assert policy is not None
    db_session.delete(policy)
    db_session.flush()
    with pytest.raises(
        access_rule_repository.AuthorizationInputUnavailable,
        match="AUTHZ_POLICY_STATE_INVALID",
    ):
        access_rule_repository.load_policy_revision(db_session)


@pytest.mark.parametrize("user_id", [None, True, 0, -1, 1.0, "1"])
def test_positive_user_id_is_strictly_validated(
    db_session: Session,
    user_id: object,
) -> None:
    from app.repositories import access_rule_repository

    with pytest.raises(ValueError, match="positive integer"):
        access_rule_repository.load_principal_set(
            db_session,
            user_id,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "permission",
    ["", " VIEW", "VIEW ", "x" * 41],
)
def test_permission_input_is_strictly_bounded(
    db_session: Session,
    permission: str,
) -> None:
    from app.domain import PrincipalSet, ResourceAncestry, ResourceRef
    from app.repositories import access_rule_repository

    with pytest.raises(ValueError, match="trimmed string"):
        access_rule_repository.load_access_rules(
            db_session,
            permission=permission,
            principals=PrincipalSet.from_ids(1),
            ancestry=ResourceAncestry((ResourceRef.global_scope(),)),
            now=datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        )


def test_naive_clock_and_unsafe_limit_configuration_are_rejected(
    db_session: Session,
) -> None:
    from app.domain import PrincipalSet, ResourceAncestry, ResourceRef
    from app.repositories import access_rule_repository

    with pytest.raises(ValueError, match="timezone-aware"):
        access_rule_repository.load_access_rules(
            db_session,
            permission="VIEW",
            principals=PrincipalSet.from_ids(1),
            ancestry=ResourceAncestry((ResourceRef.global_scope(),)),
            now=datetime(2026, 7, 27, 12),
        )
    with pytest.raises(ValueError, match="between 1"):
        access_rule_repository.AuthorizationRepositoryLimits(max_groups=0)
    with pytest.raises(ValueError, match="between 1"):
        access_rule_repository.AuthorizationRepositoryLimits(max_rules=8_193)
