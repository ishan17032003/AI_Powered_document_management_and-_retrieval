"""Request-scoped authorization boundary for search and RAG retrieval.

Search indexes are shared acceleration structures.  They are never an
authorization source: every request obtains an exact VIEW document-ID set from
the bounded ACL resolver, and results are checked against a fresh set again
before they are returned or passed to a generation provider.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .. import models
from ..repositories.visible_document_repository import (
    VisibleDocumentResolutionUnavailable,
    resolve_visible_document_ids,
)


def resolve_view_document_ids(
    db: Session,
    user: models.User,
    *,
    now: datetime | None = None,
) -> frozenset[int]:
    """Resolve exact current VIEW IDs, failing closed on unavailable input.

    The resolver intentionally runs on every request.  There is no process or
    request cache that could keep a revoked document searchable.
    """

    query_now = now or datetime.now(UTC)
    return resolve_view_document_ids_for_user_id(db, user.id, now=query_now)


def resolve_view_document_ids_for_user_id(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> frozenset[int]:
    """ID-based variant for service boundaries that already authenticated a user."""

    query_now = now or datetime.now(UTC)
    try:
        return resolve_visible_document_ids(
            db,
            user_id=user_id,
            permission="VIEW",
            now=query_now,
        )
    except (VisibleDocumentResolutionUnavailable, ValueError):
        return frozenset()


def filter_document_ids(
    values: set[int] | frozenset[int] | list[int] | tuple[int, ...],
    allowed_ids: set[int] | frozenset[int],
) -> frozenset[int]:
    """Return only positive integer IDs in the authoritative allowed set."""

    return frozenset(
        value
        for value in values
        if type(value) is int and value > 0 and value in allowed_ids
    )
