"""HTTP routes for audit reporting."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require
from ..services import audit_service
from ..utils.cursors import cursor_int, cursor_time, decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("", response_model=list[schemas.AuditOut])
def list_audit(
    action: str | None = Query(default=None, min_length=1, max_length=60),
    actor: str | None = Query(default=None, min_length=1, max_length=160),
    object_id: str | None = Query(default=None, min_length=1, max_length=40),
    limit: int = Query(default=100, ge=1, le=1000),
    user: models.User = Depends(require("VIEW_AUDIT")),
    db: Session = Depends(get_db),
):
    return audit_service.list_entries(
        db,
        action=action,
        actor=actor,
        object_id=object_id,
        limit=limit,
        user=user,
    )


@router.get("/cursor", response_model=schemas.AuditPage)
def list_audit_cursor(
    cursor: str | None = Query(default=None, max_length=256),
    action: str | None = Query(default=None, min_length=1, max_length=60),
    actor: str | None = Query(default=None, min_length=1, max_length=160),
    object_id: str | None = Query(default=None, min_length=1, max_length=40),
    limit: int = Query(default=100, ge=1, le=1000),
    user: models.User = Depends(require("VIEW_AUDIT")),
    db: Session = Depends(get_db),
):
    try:
        values = decode_cursor(cursor)
        timestamp = cursor_time(values, "timestamp") if values else None
        entry_id = cursor_int(values, "id") if values else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    entries = audit_service.list_entries_page(db, action=action, actor=actor, object_id=object_id, limit=limit + 1, cursor_time=timestamp, cursor_id=entry_id, user=user)
    has_next = len(entries) > limit
    items = entries[:limit]
    next_cursor = None
    if has_next and items:
        last = items[-1]
        next_cursor = encode_cursor(timestamp=last.timestamp.isoformat(), id=last.id)
    return schemas.AuditPage(items=items, next_cursor=next_cursor)
