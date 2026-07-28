"""Backward-compatible facade for audit logging."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from . import models
from .services.audit_service import record as _record
from .utils.request_context import get_request_context


def record(
    db: Session,
    *,
    actor: models.User | None,
    action: str,
    object_type: str = "",
    object_id: str | int = "",
    details: dict | None = None,
    request: Request | None = None,
) -> None:
    _record(
        db,
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        details=details,
        context=get_request_context(request),
    )


__all__ = ["record"]
