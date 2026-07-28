"""Database queries supporting role and permission resolution."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models


def list_assignments(
    db: Session,
    user_id: int,
) -> list[models.Assignment]:
    return (
        db.query(models.Assignment).filter(models.Assignment.user_id == user_id).all()
    )


def list_permission_codes_for_role(
    db: Session,
    role_id: int,
) -> list[str]:
    rows = (
        db.query(models.Permission.code)
        .join(
            models.RolePermission,
            models.RolePermission.permission_id == models.Permission.id,
        )
        .filter(models.RolePermission.role_id == role_id)
        .all()
    )
    return [row[0] for row in rows]


def list_role_names(
    db: Session,
    user_id: int,
    *,
    effect: str = "ALLOW",
) -> list[str]:
    rows = (
        db.query(models.Role.name)
        .join(
            models.Assignment,
            models.Assignment.role_id == models.Role.id,
        )
        .filter(
            models.Assignment.user_id == user_id,
            models.Assignment.effect == effect,
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def list_role_names_for_users(
    db: Session,
    user_ids: set[int] | list[int],
    *,
    effect: str = "ALLOW",
) -> dict[int, list[str]]:
    """Load all user role names in one bounded query for admin list views."""
    if not user_ids:
        return {}
    rows = (
        db.query(models.Assignment.user_id, models.Role.name)
        .join(models.Role, models.Role.id == models.Assignment.role_id)
        .filter(
            models.Assignment.user_id.in_(user_ids),
            models.Assignment.effect == effect,
        )
        .distinct()
        .order_by(models.Assignment.user_id.asc(), models.Role.name.asc())
        .all()
    )
    result: dict[int, list[str]] = {}
    for user_id, role_name in rows:
        result.setdefault(user_id, []).append(role_name)
    return result
