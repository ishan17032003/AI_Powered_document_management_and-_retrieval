"""Role and permission resolution."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models
from ..repositories import rbac_repository

MANAGE_KNOWLEDGE_PERMISSION = "MANAGE_KNOWLEDGE"
IMPORT_SERVER_FOLDER_PERMISSION = "IMPORT_SERVER_FOLDER"

PERMISSIONS = [
    "CREATE",
    "VIEW",
    "DOWNLOAD",
    "EDIT_METADATA",
    "EDIT_CONTENT",
    "VERSION",
    "MOVE",
    "DELETE",
    "SHARE",
    "APPROVE",
    "MANAGE_PERMISSIONS",
    "MANAGE_RETENTION",
    MANAGE_KNOWLEDGE_PERMISSION,
    IMPORT_SERVER_FOLDER_PERMISSION,
    "VIEW_AUDIT",
    "EXPORT",
    "ADMIN",
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "Super Admin": list(PERMISSIONS),
    "Administrator": [permission for permission in PERMISSIONS if permission != "ADMIN"],
    "Records Manager": [
        "VIEW",
        "DOWNLOAD",
        "CREATE",
        "EDIT_METADATA",
        "EDIT_CONTENT",
        "VERSION",
        "DELETE",
        "SHARE",
        "APPROVE",
        "MANAGE_RETENTION",
        "VIEW_AUDIT",
        "EXPORT",
    ],
    "Contributor": [
        "VIEW",
        "DOWNLOAD",
        "CREATE",
        "EDIT_METADATA",
        "EDIT_CONTENT",
        "VERSION",
        "SHARE",
    ],
    "Reviewer": ["VIEW", "DOWNLOAD", "APPROVE", "VIEW_AUDIT"],
    "Viewer": ["VIEW", "DOWNLOAD"],
    "Auditor": ["VIEW", "VIEW_AUDIT"],
    "Guest": ["VIEW"],
}


def permission_effects(
    db: Session,
    user: models.User,
) -> dict[str, str]:
    """Collapse assignments while preserving the local slice's global behavior."""
    effects: dict[str, str] = {}
    for assignment in rbac_repository.list_assignments(db, user.id):
        codes = rbac_repository.list_permission_codes_for_role(
            db,
            assignment.role_id,
        )
        for code in codes:
            if assignment.effect == "DENY":
                effects[code] = "DENY"
            elif effects.get(code) != "DENY":
                effects[code] = "ALLOW"
    return effects


def user_permissions(db: Session, user: models.User) -> set[str]:
    return {
        code
        for code, effect in permission_effects(db, user).items()
        if effect == "ALLOW"
    }


def has_permission(
    db: Session,
    user: models.User,
    permission: str,
) -> bool:
    return permission in user_permissions(db, user)


def global_permission_effects(
    db: Session,
    user: models.User,
) -> dict[str, str]:
    """Resolve capabilities that are valid only at the global scope.

    Legacy resource assignments are still used by the document slice until the
    flexible ACL repository lands. They must never be promoted into authority
    over shared, application-wide state such as the OKF bundle.
    """

    effects: dict[str, str] = {}
    for assignment in rbac_repository.list_assignments(db, user.id):
        if assignment.scope_type != "GLOBAL" or assignment.scope_id is not None:
            continue
        codes = rbac_repository.list_permission_codes_for_role(
            db,
            assignment.role_id,
        )
        for code in codes:
            if assignment.effect == "DENY":
                effects[code] = "DENY"
            elif assignment.effect == "ALLOW" and effects.get(code) != "DENY":
                effects[code] = "ALLOW"
    return effects


def has_global_permission(
    db: Session,
    user: models.User,
    permission: str,
) -> bool:
    """Return whether an explicit global allow survives global deny precedence."""

    return global_permission_effects(db, user).get(permission) == "ALLOW"


def user_roles(db: Session, user: models.User) -> list[str]:
    return rbac_repository.list_role_names(db, user.id)


def viewable_document_ids(
    db: Session,
    user: models.User,
) -> set[int] | None:
    """Return all-or-none visibility until resource scopes are implemented."""
    if has_permission(db, user, "VIEW"):
        return None
    return set()
