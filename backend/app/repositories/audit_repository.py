"""Database access for audit entries."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models


def add(db: Session, entry: models.AuditLog) -> models.AuditLog:
    db.add(entry)
    return entry


def list_entries(
    db: Session,
    *,
    action: str | None,
    actor: str | None,
    object_id: str | None,
    limit: int,
    document_ids: frozenset[int] | set[int] | tuple[int, ...] | None = None,
) -> list[models.AuditLog]:
    """Return a bounded, optionally document-scoped audit projection.

    ``document_ids`` is an authorization boundary, not a presentation filter:
    when supplied the query is restricted to document rows whose stored object
    identifier is in the exact, already-resolved visible set.  Non-document
    rows are deliberately excluded.  An empty set is a safe empty result and
    never falls back to an unscoped query.
    """
    query = db.query(models.AuditLog)
    if action:
        query = query.filter(models.AuditLog.action == action)
    if actor:
        query = query.filter(models.AuditLog.actor_name == actor)
    if object_id:
        query = query.filter(models.AuditLog.object_id == object_id)
    if document_ids is not None:
        # The resolver validates positive integer IDs.  Repeat the validation at
        # the repository boundary so a malformed caller cannot broaden the
        # query (or make SQLite coerce arbitrary values).
        normalized_ids = tuple(
            sorted(
                {
                    value
                    for value in document_ids
                    if type(value) is int and value > 0
                }
            )
        )
        if not normalized_ids:
            return []
        query = query.filter(
            models.AuditLog.object_type == "document",
            models.AuditLog.object_id.in_(tuple(str(value) for value in normalized_ids)),
        )
    return query.order_by(models.AuditLog.timestamp.desc()).limit(limit).all()


def list_entries_after(
    db: Session,
    *,
    action: str | None,
    actor: str | None,
    object_id: str | None,
    timestamp: datetime | None,
    entry_id: int | None,
    limit: int,
    document_ids: frozenset[int] | set[int] | tuple[int, ...] | None = None,
) -> list[models.AuditLog]:
    query = db.query(models.AuditLog)
    if action:
        query = query.filter(models.AuditLog.action == action)
    if actor:
        query = query.filter(models.AuditLog.actor_name == actor)
    if object_id:
        query = query.filter(models.AuditLog.object_id == object_id)
    if timestamp is not None and entry_id is not None:
        query = query.filter(
            or_(
                models.AuditLog.timestamp < timestamp,
                (models.AuditLog.timestamp == timestamp) & (models.AuditLog.id < entry_id),
            )
        )
    if document_ids is not None:
        normalized_ids = tuple(sorted({value for value in document_ids if type(value) is int and value > 0}))
        if not normalized_ids:
            return []
        query = query.filter(
            models.AuditLog.object_type == "document",
            models.AuditLog.object_id.in_(tuple(str(value) for value in normalized_ids)),
        )
    return query.order_by(models.AuditLog.timestamp.desc(), models.AuditLog.id.desc()).limit(limit).all()
