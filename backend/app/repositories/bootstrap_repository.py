"""Transaction-neutral persistence for the bootstrap permission catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy.orm import Session

from .. import models


def ensure_permissions(
    db: Session,
    codes: Sequence[str],
) -> dict[str, models.Permission]:
    existing = {
        permission.code: permission for permission in db.query(models.Permission)
    }
    for code in codes:
        if code not in existing:
            permission = models.Permission(code=code)
            db.add(permission)
            existing[code] = permission
    db.flush()
    return existing


def ensure_roles(
    db: Session,
    permissions: dict[str, models.Permission],
    role_permissions: Mapping[str, Sequence[str]],
) -> dict[str, models.Role]:
    roles = {role.name: role for role in db.query(models.Role)}
    for name, codes in role_permissions.items():
        role = roles.get(name)
        if role is None:
            role = models.Role(
                name=name,
                description=f"{name} (default)",
                is_system=True,
            )
            db.add(role)
            db.flush()
            roles[name] = role

        existing_codes = {
            role_permission.permission.code
            for role_permission in db.query(models.RolePermission)
            .filter(models.RolePermission.role_id == role.id)
            .all()
        }
        for code in codes:
            if code not in existing_codes and code in permissions:
                db.add(
                    models.RolePermission(
                        role_id=role.id,
                        permission_id=permissions[code].id,
                    )
                )
    db.flush()
    return roles


def ensure_hierarchy(db: Session) -> None:
    cabinet = db.query(models.Cabinet).first()
    if cabinet is None:
        cabinet = models.Cabinet(name="General")
        db.add(cabinet)
        db.flush()
    if db.query(models.Folder).first() is None:
        db.add(models.Folder(cabinet_id=cabinet.id, name="Inbox"))
    db.flush()
