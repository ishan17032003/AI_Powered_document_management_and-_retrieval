"""Ingestion status and administrator control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_user, require_global
from ..services import ingestion_service
from ..utils.request_context import bound_request_context, get_request_context

router = APIRouter(prefix="/api/v1/ingestions", tags=["ingestions"])


@router.get("/{job_id}", response_model=schemas.IngestionStatusOut)
def get_ingestion_status(
    job_id: str = Path(min_length=1, max_length=36),
    actor: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ingestion_service.get_status(db, actor, job_id)


@router.post("/{job_id}/retry", response_model=schemas.IngestionStatusOut)
def retry_ingestion(
    request: Request,
    job_id: str = Path(min_length=1, max_length=36),
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        return ingestion_service.retry(
            db,
            actor,
            job_id,
            context=context,
        )


@router.post("/{job_id}/cancel", response_model=schemas.IngestionStatusOut)
def cancel_ingestion(
    request: Request,
    job_id: str = Path(min_length=1, max_length=36),
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        return ingestion_service.cancel(
            db,
            actor,
            job_id,
            context=context,
        )
