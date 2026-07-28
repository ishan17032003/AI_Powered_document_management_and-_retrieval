"""Outbox-driven cleanup for document tombstones."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .. import models
from ..repositories import document_repository, job_repository
from ..utils import file_storage


def process_cleanup_event(db: Session, *, event_id: str) -> models.OutboxEvent:
    event = job_repository.get_outbox_event(db, event_id)
    if event is None:
        raise ValueError("cleanup event not found")
    if event.event_type != "document.storage.cleanup.requested":
        raise ValueError("unsupported cleanup event")
    if event.state == "PROCESSED":
        return event
    if event.state not in {"PENDING", "CLAIMED"}:
        raise ValueError("cleanup event is not executable")
    try:
        payload = json.loads(event.payload)
        document_id = int(payload["document_id"])
        file_keys = payload["file_keys"]
        if not isinstance(file_keys, list) or len(file_keys) > 500:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        event.state = "DEAD"
        event.last_error_code = "CLEANUP_PAYLOAD_INVALID"
        event.last_error_message = "cleanup payload rejected"
        db.commit()
        raise ValueError("cleanup payload invalid") from exc
    for key in file_keys:
        if not isinstance(key, str) or len(key) > 300:
            continue
        if document_repository.active_references_for_key(
            db, file_key=key, excluding_document_id=document_id
        ) == 0:
            file_storage.delete_file(key)
    event.state = "PROCESSED"
    event.processed_at = datetime.now(UTC)
    event.updated_at = datetime.now(UTC)
    db.commit()
    return event


__all__ = ["process_cleanup_event"]
