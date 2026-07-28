"""HTTP routes for exact-duplicate management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require
from ..services import duplicate_service
from ..utils.request_context import get_request_context
from ..utils.cursors import cursor_int, decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1/duplicates", tags=["duplicates"])


@router.get("", response_model=list[schemas.DupGroupOut])
def list_duplicate_groups(
    include_resolved: bool = False,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    return duplicate_service.list_groups(
        db,
        include_resolved=include_resolved,
        user=user,
    )


@router.get("/cursor", response_model=schemas.DuplicatePage)
def list_duplicate_groups_cursor(
    include_resolved: bool = False,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=500),
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    try:
        values = decode_cursor(cursor)
        after_id = cursor_int(values, "id") if values else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    items, next_id = duplicate_service.list_groups_page(db, include_resolved=include_resolved, user=user, after_id=after_id, limit=limit)
    return schemas.DuplicatePage(items=items, next_cursor=encode_cursor(id=next_id) if next_id else None)


@router.post("/{group_id}/resolve", response_model=schemas.DupGroupOut)
def resolve_group(
    group_id: Annotated[int, Path(ge=1)],
    payload: schemas.ResolveDup,
    request: Request,
    user: models.User = Depends(require("DELETE")),
    db: Session = Depends(get_db),
):
    return duplicate_service.resolve_group(
        db,
        user,
        group_id,
        payload,
        context=get_request_context(request),
    )
