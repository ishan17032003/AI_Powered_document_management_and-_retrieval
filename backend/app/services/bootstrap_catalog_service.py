"""Transaction-neutral creation of the current permission and hierarchy catalog."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models
from ..repositories import bootstrap_repository
from . import rbac_service


def ensure_permissions(db: Session) -> dict[str, models.Permission]:
    """Ensure permission rows without committing the caller's transaction."""

    return bootstrap_repository.ensure_permissions(db, rbac_service.PERMISSIONS)


def ensure_roles(
    db: Session,
    permissions: dict[str, models.Permission],
) -> dict[str, models.Role]:
    """Ensure system role bundles without committing the caller's transaction."""

    return bootstrap_repository.ensure_roles(
        db,
        permissions,
        rbac_service.ROLE_PERMISSIONS,
    )


def ensure_hierarchy(db: Session) -> None:
    """Ensure the minimal current cabinet/folder hierarchy without committing."""

    bootstrap_repository.ensure_hierarchy(db)
