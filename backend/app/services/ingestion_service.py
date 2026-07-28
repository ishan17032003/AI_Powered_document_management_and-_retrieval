"""Authorized ingestion status and operator-control use cases."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .. import models, schemas
from ..repositories import job_repository
from ..utils.request_context import RequestContext
from . import (
    audit_service,
    ingestion_pipeline,
    ingestion_worker,
    rbac_service,
    search_authorization,
)
from .exceptions import ConflictError, NotFoundError

_RETRYABLE_STATES = frozenset({"REVIEW", "FAILED", "DEAD", "CANCELLED"})
_CANCELLABLE_STATES = frozenset({"PENDING", "RUNNING", "REVIEW"})
_TERMINAL_STATES = frozenset(
    {"SUCCEEDED", "REVIEW", "FAILED", "DEAD", "CANCELLED"}
)


def _job(db: Session, job_id: str) -> models.IngestionJob:
    try:
        job = job_repository.get_ingestion_job(db, job_id)
    except ValueError as exc:
        raise NotFoundError("Ingestion not found") from exc
    if job is None:
        raise NotFoundError("Ingestion not found")
    return job


def _quality_signals(version: models.DocVersion) -> dict[str, bool | float | int | str]:
    try:
        parsed = json.loads(version.extraction_quality_signals or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        key: value
        for key, value in parsed.items()
        if isinstance(key, str)
        and isinstance(value, (bool, float, int, str))
        and len(key) <= 40
        and (not isinstance(value, str) or len(value) <= 80)
    }


def _projection(job: models.IngestionJob) -> schemas.IngestionStatusOut:
    version = job.version
    extraction = None
    if version is not None and (
        version.extraction_method is not None
        or version.extraction_completed_at is not None
    ):
        extraction = schemas.ExtractionProvenanceOut(
            method=version.extraction_method,
            extractor_name=version.extractor_name,
            extractor_version=version.extractor_version,
            ocr_engine=version.ocr_engine,
            ocr_engine_version=version.ocr_engine_version,
            ocr_languages=(
                version.ocr_languages.split("+") if version.ocr_languages else []
            ),
            quality_score=version.extraction_quality_score,
            quality_signals=_quality_signals(version),
            completed_at=version.extraction_completed_at,
        )
    return schemas.IngestionStatusOut(
        id=job.id,
        document_id=job.document_id,
        version_id=job.version_id,
        state=job.state,
        stage=job.stage,
        stage_version=job.stage_version,
        attempt_count=job.attempt_count,
        retryable=job.state in _RETRYABLE_STATES,
        cancellable=job.state in _CANCELLABLE_STATES,
        terminal=job.state in _TERMINAL_STATES,
        next_attempt_at=job.next_attempt_at,
        error_code=job.error_code,
        document_status=job.document.status if job.document is not None else None,
        stage_results=ingestion_pipeline.stage_results(job),
        degraded_stages=ingestion_pipeline.degraded_stages(job),
        extraction=extraction,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )


def get_status(
    db: Session,
    actor: models.User,
    job_id: str,
) -> schemas.IngestionStatusOut:
    job = _job(db, job_id)
    is_admin = rbac_service.has_global_permission(db, actor, "ADMIN")
    visible_ids = (
        frozenset()
        if job.document_id is None
        else search_authorization.resolve_view_document_ids(db, actor)
    )
    if not is_admin and job.document_id not in visible_ids:
        raise NotFoundError("Ingestion not found")
    return _projection(job)


def retry(
    db: Session,
    actor: models.User,
    job_id: str,
    *,
    context: RequestContext | None = None,
) -> schemas.IngestionStatusOut:
    existing = _job(db, job_id)
    if existing.state not in _RETRYABLE_STATES:
        raise ConflictError("Ingestion is not retryable")
    try:
        job = ingestion_worker.retry_job(db, job_id=job_id)
        audit_service.record(
            db,
            actor=actor,
            action="INGESTION_RETRY",
            object_type="document",
            object_id=job.document_id or "",
            context=context,
        )
        db.commit()
        db.refresh(job)
    except ValueError as exc:
        db.rollback()
        raise ConflictError("Ingestion is not retryable") from exc
    return _projection(job)


def cancel(
    db: Session,
    actor: models.User,
    job_id: str,
    *,
    context: RequestContext | None = None,
) -> schemas.IngestionStatusOut:
    existing = _job(db, job_id)
    if existing.state == "CANCELLED":
        return _projection(existing)
    if existing.state not in _CANCELLABLE_STATES:
        raise ConflictError("Ingestion is not cancellable")
    try:
        job = ingestion_worker.cancel_job(db, job_id=job_id)
        audit_service.record(
            db,
            actor=actor,
            action="INGESTION_CANCEL",
            object_type="document",
            object_id=job.document_id or "",
            context=context,
        )
        db.commit()
        db.refresh(job)
    except ValueError as exc:
        db.rollback()
        raise ConflictError("Ingestion is not cancellable") from exc
    return _projection(job)


__all__ = ["cancel", "get_status", "retry"]
