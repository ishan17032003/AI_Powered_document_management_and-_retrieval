"""Application-layer authorization for shared OKF mutation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app import models


def _assign_capabilities(
    db: Session,
    user: models.User,
    permission_codes: Iterable[str],
    *,
    scope_type: str = "GLOBAL",
    scope_id: int | None = None,
    effect: str = "ALLOW",
) -> None:
    from app import models

    permissions: list[models.Permission] = []
    for code in set(permission_codes):
        permission = (
            db.query(models.Permission)
            .filter(models.Permission.code == code)
            .one_or_none()
        )
        if permission is None:
            permission = models.Permission(code=code)
            db.add(permission)
        permissions.append(permission)
    role = models.Role(name=f"opaque-bundle-{user.id}")
    db.add(role)
    db.flush()
    db.add_all(
        models.RolePermission(
            role_id=role.id,
            permission_id=permission.id,
        )
        for permission in permissions
    )
    db.add(
        models.Assignment(
            user_id=user.id,
            role_id=role.id,
            scope_type=scope_type,
            scope_id=scope_id,
            effect=effect,
        )
    )
    db.flush()


def test_permission_catalog_grants_knowledge_management_to_admin_bundles(
    db_session: Session,
) -> None:
    from app.services import bootstrap_catalog_service, rbac_service

    permissions = bootstrap_catalog_service.ensure_permissions(db_session)
    roles = bootstrap_catalog_service.ensure_roles(db_session, permissions)
    capability = rbac_service.MANAGE_KNOWLEDGE_PERMISSION

    assert capability in permissions
    assert capability in rbac_service.ROLE_PERMISSIONS["Super Admin"]
    assert capability in rbac_service.ROLE_PERMISSIONS["Administrator"]
    assert capability not in rbac_service.ROLE_PERMISSIONS["Contributor"]
    assert capability not in rbac_service.ROLE_PERMISSIONS["Records Manager"]
    assert {row.permission.code for row in roles["Administrator"].permissions} >= {
        capability
    }


def test_create_only_user_is_denied_before_application_mutation(
    db_session: Session,
    user_factory: Callable[..., models.User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import okf
    from app.services import okf_service, search_application_service
    from app.services.exceptions import PermissionDeniedError

    user = user_factory()
    _assign_capabilities(db_session, user, {"CREATE"})
    mutation_calls: list[str] = []
    monkeypatch.setattr(
        okf_service,
        "create_entry",
        lambda *_args, **_kwargs: mutation_calls.append("create"),
    )
    monkeypatch.setattr(
        okf_service,
        "reload_bundle",
        lambda: mutation_calls.append("reload"),
    )

    expected = "Missing required permission: MANAGE_KNOWLEDGE"
    with pytest.raises(PermissionDeniedError, match=expected):
        search_application_service.create_okf_entry(
            db_session,
            user,
            filename="blocked.md",
            content="blocked",
        )
    with pytest.raises(PermissionDeniedError, match=expected):
        search_application_service.reload_okf_bundle(db_session, user)
    with pytest.raises(PermissionDeniedError, match=expected):
        okf.save_entry(
            db_session,
            user,
            "blocked-via-facade.md",
            "blocked",
        )
    with pytest.raises(PermissionDeniedError, match=expected):
        okf.reload_bundle(db_session, user)

    assert mutation_calls == []


@pytest.mark.parametrize(
    ("scope_type", "scope_id"),
    [
        ("CABINET", 11),
        ("FOLDER", 12),
        ("DOC", 13),
        ("GLOBAL", 14),
    ],
)
def test_resource_scoped_capability_cannot_mutate_shared_knowledge(
    db_session: Session,
    user_factory: Callable[..., models.User],
    monkeypatch: pytest.MonkeyPatch,
    scope_type: str,
    scope_id: int,
) -> None:
    from app.services import okf_service, rbac_service, search_application_service
    from app.services.exceptions import PermissionDeniedError

    user = user_factory()
    capability = rbac_service.MANAGE_KNOWLEDGE_PERMISSION
    _assign_capabilities(
        db_session,
        user,
        {capability},
        scope_type=scope_type,
        scope_id=scope_id,
    )
    mutation_calls: list[str] = []
    monkeypatch.setattr(
        okf_service,
        "reload_bundle",
        lambda: mutation_calls.append("reload"),
    )

    # The legacy resolver still sees resource assignments. The shared-state
    # resolver must not promote them to an application-wide capability.
    assert rbac_service.has_permission(db_session, user, capability)
    assert not rbac_service.has_global_permission(db_session, user, capability)
    with pytest.raises(
        PermissionDeniedError,
        match="Missing required permission: MANAGE_KNOWLEDGE",
    ):
        search_application_service.reload_okf_bundle(db_session, user)
    assert mutation_calls == []


def test_explicit_global_deny_overrides_global_allow(
    db_session: Session,
    user_factory: Callable[..., models.User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models
    from app.services import okf_service, rbac_service, search_application_service
    from app.services.exceptions import PermissionDeniedError

    user = user_factory()
    capability = rbac_service.MANAGE_KNOWLEDGE_PERMISSION
    _assign_capabilities(db_session, user, {capability})
    allowed_assignment = (
        db_session.query(models.Assignment)
        .filter(models.Assignment.user_id == user.id)
        .one()
    )
    db_session.add(
        models.Assignment(
            user_id=user.id,
            role_id=allowed_assignment.role_id,
            scope_type="GLOBAL",
            scope_id=None,
            effect="DENY",
        )
    )
    db_session.flush()
    mutation_calls: list[str] = []
    monkeypatch.setattr(
        okf_service,
        "reload_bundle",
        lambda: mutation_calls.append("reload"),
    )

    assert not rbac_service.has_global_permission(db_session, user, capability)
    with pytest.raises(
        PermissionDeniedError,
        match="Missing required permission: MANAGE_KNOWLEDGE",
    ):
        search_application_service.reload_okf_bundle(db_session, user)
    assert mutation_calls == []


def test_manage_knowledge_user_can_use_application_mutation_paths(
    db_session: Session,
    user_factory: Callable[..., models.User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import okf
    from app.services import okf_service, rbac_service, search_application_service

    user = user_factory()
    capability = rbac_service.MANAGE_KNOWLEDGE_PERMISSION
    _assign_capabilities(db_session, user, {capability})
    mutation_calls: list[tuple[str, ...]] = []

    def fake_create(filename: str, content: str) -> dict:
        mutation_calls.append(("create", filename, content))
        return {
            "status": "saved",
            "filename": filename,
            "title": "Allowed",
            "bundle_size": 1,
        }

    def fake_reload() -> int:
        mutation_calls.append(("reload",))
        return 1

    monkeypatch.setattr(okf_service, "create_entry", fake_create)
    monkeypatch.setattr(okf_service, "reload_bundle", fake_reload)
    monkeypatch.setattr(
        search_application_service.audit_service,
        "record",
        lambda *_args, **_kwargs: None,
    )

    direct_result = search_application_service.create_okf_entry(
        db_session,
        user,
        filename="direct.md",
        content="direct",
    )
    assert direct_result["status"] == "saved"
    assert search_application_service.reload_okf_bundle(db_session, user) == 1

    facade_result = okf.save_entry(
        db_session,
        user,
        "facade.md",
        "facade",
    )
    assert facade_result["status"] == "saved"
    assert okf.reload_bundle(db_session, user) == 1
    assert mutation_calls == [
        ("create", "direct.md", "direct"),
        ("reload",),
        ("create", "facade.md", "facade"),
        ("reload",),
    ]
