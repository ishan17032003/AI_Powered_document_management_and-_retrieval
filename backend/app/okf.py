"""Compatibility facade for Open Knowledge Format bundle operations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from . import models
from .services import search_application_service
from .services.okf_service import OkfEntry, bundle_status, get_bundle, query_okf
from .utils.request_context import RequestContext


def save_entry(
    db: Session,
    user: models.User,
    filename: str,
    content: str,
    *,
    context: RequestContext | None = None,
) -> dict:
    """Save shared knowledge through the capability-checked application service."""
    return search_application_service.create_okf_entry(
        db,
        user,
        filename=filename,
        content=content,
        context=context,
    )


def reload_bundle(
    db: Session,
    user: models.User,
    *,
    context: RequestContext | None = None,
) -> int:
    """Reload shared knowledge through the capability-checked application service."""
    return search_application_service.reload_okf_bundle(
        db,
        user,
        context=context,
    )


__all__ = [
    "OkfEntry",
    "bundle_status",
    "get_bundle",
    "query_okf",
    "reload_bundle",
    "save_entry",
]
