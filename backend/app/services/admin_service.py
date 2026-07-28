"""Administration and dashboard use cases."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import schemas
from ..repositories import admin_repository, rbac_repository, user_repository
from . import extraction_service, rbac_service


def list_users(db: Session) -> list[schemas.UserAdminOut]:
    users = user_repository.list_all(db)
    roles = rbac_repository.list_role_names_for_users(db, {user.id for user in users})
    return [
        schemas.UserAdminOut(
            id=user.id,
            username=user.username,
            name=user.name,
            email=user.email,
            status=user.status,
            roles=roles.get(user.id, []),
        )
        for user in users
    ]


def list_users_page(db: Session, *, after_id: int | None, limit: int) -> tuple[list[schemas.UserAdminOut], int | None]:
    users = user_repository.list_after_id(db, after_id=after_id, limit=limit + 1)
    roles = rbac_repository.list_role_names_for_users(db, {user.id for user in users})
    outputs = [schemas.UserAdminOut(id=user.id, username=user.username, name=user.name, email=user.email, status=user.status, roles=roles.get(user.id, [])) for user in users]
    return outputs[:limit], (outputs[limit - 1].id if len(outputs) > limit else None)


def rbac_matrix() -> dict:
    return {
        "permissions": rbac_service.PERMISSIONS,
        "roles": {
            role: list(permissions)
            for role, permissions in rbac_service.ROLE_PERMISSIONS.items()
        },
    }


def dashboard_stats(
    db: Session,
    *,
    visible_document_ids: set[int] | frozenset[int],
) -> dict:
    return {
        **admin_repository.dashboard_counts(
            db,
            visible_document_ids=visible_document_ids,
        ),
        "engine": extraction_service.engine_status(),
    }
