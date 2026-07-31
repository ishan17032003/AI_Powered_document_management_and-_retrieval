"""Durable ingestion stage handlers.

The request transaction only creates the document, immutable object reference,
and pending job.  This module owns the expensive and retryable EXTRACT and
INDEX stages.  Each stage commits its cursor before the next stage starts so a
crash resumes from the durable stage rather than repeating the whole pipeline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..observability import trace_span
from ..repositories import document_repository, job_repository, search_repository
from ..storage import object_store
from ..utils import classification
from . import (
    extraction_service,
    ingestion_pipeline,
    search_service,
    visual_processing_service,
)

EXTRACTOR_VERSION = extraction_service.EXTRACTION_PIPELINE_VERSION
CHUNKER_VERSION = "document-v1"
INDEX_VERSION = "fts5-v1"


def mandatory_stages_complete(
    document: models.Document,
    version: models.DocVersion,
    job: models.IngestionJob | None = None,
) -> bool:
    """Return whether every required stage has durable success evidence."""
    # The stage results are the durable completion contract; metadata alone is
    # insufficient because a crash can occur between a side effect and cursor
    # persistence.
    if job is None:
        return (
            document.status == "PROCESSING"
            and document.ocr_status not in {"pending", "error", "unavailable", "skipped"}
            and bool(version.ocr_text or document.page_count == 0)
            and version.storage_state == "AVAILABLE"
            and version.index_version == INDEX_VERSION
        )
    decision = ingestion_pipeline.evaluate_readiness(job)
    return (
        document.status == "PROCESSING"
        and document.ocr_status not in {"pending", "error", "unavailable", "skipped"}
        and bool(version.ocr_text or document.page_count == 0)
        and version.storage_state == "AVAILABLE"
        and decision.ready
    )


def embedding_version() -> str:
    if not settings.enable_embeddings:
        return "disabled-v1"
    return f"embedding:{settings.embedding_model[:60]}"


def _extraction_requires_review(
    extraction: extraction_service.OcrResult,
) -> tuple[bool, str | None]:
    if extraction.status in {"unavailable", "error", "skipped"}:
        return True, "EXTRACTION_UNAVAILABLE"
    if not extraction.text.strip():
        return True, "EXTRACTION_EMPTY"
    if extraction.missing_ocr_languages:
        return True, "OCR_LANGUAGE_PACK_MISSING"
    if (
        extraction.quality_score is None
        or extraction.quality_score < settings.extraction_review_quality_threshold
    ):
        return True, "EXTRACTION_QUALITY_LOW"
    return False, None


def _fail(
    db: Session,
    job: models.IngestionJob,
    document: models.Document | None,
    code: str,
) -> None:
    if job.state == "CANCELLED":
        return
    failed_stage = (
        ingestion_pipeline.IngestionStage.EXTRACT
        if job.stage == "EXTRACT"
        else ingestion_pipeline.IngestionStage.INDEX
    )
    ingestion_pipeline.record_stage_result(
        job,
        failed_stage,
        ingestion_pipeline.StageResultStatus.FAILED,
        code=code,
    )
    job.state = "FAILED"
    job.error_code = code[:80]
    job.error_message = "ingestion stage failed"
    job.lock_owner = None
    job.locked_at = None
    if document is not None:
        document.status = "ERROR"
        document.failure_code = code[:80]
    db.commit()


def _is_cancelled(db: Session, job: models.IngestionJob) -> bool:
    db.refresh(job)
    return job.state == "CANCELLED"


def run_claimed_job(db: Session, job: models.IngestionJob) -> models.IngestionJob:
    """Run all remaining stages for an already claimed job."""
    if job.state == "CANCELLED":
        return job
    document = db.get(models.Document, job.document_id) if job.document_id else None
    version = db.get(models.DocVersion, job.version_id) if job.version_id else None
    if document is None or version is None:
        _fail(db, job, document, "INGESTION_REFERENCE_MISSING")
        return job
    try:
        if job.stage == "EXTRACT":
            with trace_span("worker", "extract", document_id=document.id):
                with object_store.open(version.file_key) as handle:
                    extraction = extraction_service.extract_text(
                        Path(handle.name),
                        version.content_type,
                        filename=version.filename,
                    )
            version.ocr_text = extraction.text
            version.extractor_version = extraction.extractor_version
            version.chunker_version = CHUNKER_VERSION
            document.ocr_status = extraction.status
            document.ocr_confidence = extraction.confidence
            document.page_count = extraction.page_count
            document.language = extraction.language
            class_name, confidence = classification.classify(extraction.text)
            document.doc_class = document_repository.get_or_create_class(db, class_name)
            document.class_confidence = confidence
            if settings.storage_backend == "minio":
                try:
                    new_key = object_store.move_to_class(version.file_key, class_name)
                    version.file_key = new_key
                except Exception:
                    pass
            version.extraction_method = extraction.status[:20]
            version.extractor_name = (
                extraction.extractor_name[:40]
                if extraction.extractor_name
                else None
            )
            version.ocr_engine = (
                extraction.ocr_engine[:40] if extraction.ocr_engine else None
            )
            version.ocr_engine_version = (
                extraction.ocr_engine_version[:40]
                if extraction.ocr_engine_version
                else None
            )
            version.ocr_languages = (
                "+".join(extraction.ocr_languages)[:40]
                if extraction.ocr_languages
                else None
            )
            version.extraction_quality_score = extraction.quality_score
            version.extraction_quality_signals = json.dumps(
                extraction.quality_signals,
                separators=(",", ":"),
                ensure_ascii=True,
                sort_keys=True,
            )
            version.extraction_completed_at = datetime.now(UTC)
            # Visual derivatives are optional for document readiness, but their
            # state is durable so text-to-image/page search can be rebuilt and
            # monitored without making the upload request do visual work.
            try:
                visual_result = visual_processing_service.process_version_visuals(
                    db,
                    document=document,
                    version=version,
                )
                visual_status = (
                    ingestion_pipeline.StageResultStatus.COMPLETED
                    if visual_result.state == "ready"
                    else ingestion_pipeline.StageResultStatus.DISABLED
                )
                ingestion_pipeline.record_optional_stage(
                    job,
                    visual_processing_service.VISUAL_STAGE_NAME,
                    visual_status,
                    code=(
                        None
                        if visual_status is ingestion_pipeline.StageResultStatus.COMPLETED
                        else "VISUAL_SOURCE_UNSUPPORTED"
                    ),
                    metrics={
                        "asset_count": visual_result.asset_count,
                        "extraction_count": visual_result.extraction_count,
                        "mode": visual_result.mode,
                    },
                )
                semantic_status = (
                    ingestion_pipeline.StageResultStatus.COMPLETED
                    if visual_result.semantic_state == "ready"
                    else ingestion_pipeline.StageResultStatus.DISABLED
                    if visual_result.semantic_state == "disabled"
                    else ingestion_pipeline.StageResultStatus.DEGRADED
                )
                ingestion_pipeline.record_optional_stage(
                    job,
                    "VISUAL_SEMANTIC_INDEXING",
                    semantic_status,
                    code=(
                        None
                        if semantic_status is ingestion_pipeline.StageResultStatus.COMPLETED
                        else visual_result.semantic_error_code or "VISUAL_SEMANTIC_UNAVAILABLE"
                    ),
                    metrics={
                        "image_count": visual_result.semantic_image_count,
                        "page_count": visual_result.semantic_page_count,
                        "mode": visual_result.mode,
                    },
                )
            except Exception:
                ingestion_pipeline.record_optional_stage(
                    job,
                    visual_processing_service.VISUAL_STAGE_NAME,
                    ingestion_pipeline.StageResultStatus.DEGRADED,
                    code="VISUAL_PROCESSING_FAILED",
                )
            review_required, review_code = _extraction_requires_review(extraction)
            extraction_stage_status = (
                ingestion_pipeline.StageResultStatus.REVIEW
                if review_required
                else ingestion_pipeline.StageResultStatus.COMPLETED
            )
            ingestion_pipeline.record_stage_result(
                job,
                ingestion_pipeline.IngestionStage.EXTRACT,
                extraction_stage_status,
                code=review_code,
                metrics={
                    "quality_score": extraction.quality_score or 0.0,
                    "page_count": extraction.page_count,
                    "language": extraction.language,
                },
            )
            ingestion_pipeline.record_stage_result(
                job,
                ingestion_pipeline.IngestionStage.CLASSIFY,
                ingestion_pipeline.StageResultStatus.COMPLETED,
                metrics={
                    "classifier": "rules-v1",
                    "classification_confidence": confidence,
                },
            )
            ingestion_pipeline.record_stage_result(
                job,
                ingestion_pipeline.IngestionStage.CHUNK,
                extraction_stage_status,
                code=review_code,
                metrics={
                    "chunker": CHUNKER_VERSION,
                    "chunk_count": 1 if extraction.text.strip() else 0,
                },
            )
            document.status = "REVIEW" if review_required else "PROCESSING"
            document.failure_code = review_code
            if review_required:
                job.error_code = review_code
                job.error_message = "manual review required"
            job.stage = "INDEX"
            job.updated_at = datetime.now(UTC)
            db.commit()
            if _is_cancelled(db, job):
                return job
        if job.stage == "INDEX":
            with trace_span("worker", "index", document_id=document.id):
                search_repository.upsert_document(
                    db,
                    document_id=document.id,
                    title=document.title,
                    content=version.ocr_text,
                )
            version.embedding_version = embedding_version()
            version.index_version = INDEX_VERSION
            ingestion_pipeline.record_stage_result(
                job,
                ingestion_pipeline.IngestionStage.INDEX,
                ingestion_pipeline.StageResultStatus.COMPLETED,
                metrics={"index": INDEX_VERSION},
            )
            if settings.enable_embeddings:
                vector_indexed = search_service.index_vector(
                    document.id,
                    document.title,
                    version.ocr_text,
                )
                ingestion_pipeline.record_optional_stage(
                    job,
                    "VECTOR_INDEXING",
                    (
                        ingestion_pipeline.StageResultStatus.COMPLETED
                        if vector_indexed
                        else ingestion_pipeline.StageResultStatus.DEGRADED
                    ),
                    code=None if vector_indexed else "VECTOR_INDEX_UNAVAILABLE",
                )
            else:
                ingestion_pipeline.record_optional_stage(
                    job,
                    "VECTOR_INDEXING",
                    ingestion_pipeline.StageResultStatus.DISABLED,
                    code="VECTOR_INDEX_DISABLED",
                )
            readiness = ingestion_pipeline.evaluate_readiness(job)
            if readiness.ready and mandatory_stages_complete(document, version, job):
                document.status = "READY"
                document.failure_code = None
                target_state = "SUCCEEDED"
                target_error_code = None
                target_error_message = None
            elif readiness.review_required or document.status == "REVIEW":
                document.status = "REVIEW"
                document.failure_code = (
                    document.failure_code or "INGESTION_DEGRADED"
                )
                target_state = "REVIEW"
                target_error_code = job.error_code or "INGESTION_DEGRADED"
                target_error_message = "manual review required"
            else:
                document.status = "ERROR"
                document.failure_code = "MANDATORY_STAGE_INCOMPLETE"
                target_state = "FAILED"
                target_error_code = "MANDATORY_STAGE_INCOMPLETE"
                target_error_message = "ingestion stage failed"
            now = datetime.now(UTC)
            db.flush()
            completion = db.execute(
                update(models.IngestionJob)
                .where(
                    models.IngestionJob.id == job.id,
                    models.IngestionJob.state.in_(("PENDING", "RUNNING")),
                )
                .values(
                    state=target_state,
                    error_code=target_error_code,
                    error_message=target_error_message,
                    completed_at=now,
                    lock_owner=None,
                    locked_at=None,
                    updated_at=now,
                )
            )
            if getattr(completion, "rowcount", 0) != 1:
                db.rollback()
                db.refresh(job)
                return job
            db.commit()
            db.refresh(job)
    except Exception:
        db.rollback()
        fresh = db.get(models.IngestionJob, job.id)
        if fresh is not None:
            _fail(db, fresh, db.get(models.Document, fresh.document_id) if fresh.document_id else None, "INGESTION_STAGE_FAILED")
            job = fresh
    return job


def run_next_job(db: Session, *, owner: str | None = None) -> models.IngestionJob | None:
    """Claim and execute one job; deployment orchestration controls looping."""
    claimed = job_repository.claim_ingestion_job(db, owner=owner or f"ingestion-{uuid4().hex}")
    if claimed is None:
        return None
    return run_claimed_job(db, claimed)


def retry_job(db: Session, *, job_id: str) -> models.IngestionJob:
    job = db.get(models.IngestionJob, job_id)
    if job is None:
        raise ValueError("job not found")
    if job.state not in {"FAILED", "DEAD", "REVIEW", "CANCELLED"}:
        raise ValueError("job is not retryable")
    results = ingestion_pipeline.stage_results(job)
    extract_result = results.get(ingestion_pipeline.IngestionStage.EXTRACT.value)
    job.stage = (
        "INDEX"
        if isinstance(extract_result, dict)
        and extract_result.get("status")
        == ingestion_pipeline.StageResultStatus.COMPLETED.value
        else "EXTRACT"
    )
    ingestion_pipeline.clear_retry_results(job)
    now = datetime.now(UTC)
    job.state = "PENDING"
    job.error_code = None
    job.error_message = None
    job.lock_owner = None
    job.locked_at = None
    job.next_attempt_at = now
    job.completed_at = None
    job.updated_at = now
    document = db.get(models.Document, job.document_id) if job.document_id else None
    if document is not None:
        document.status = "PROCESSING"
        document.failure_code = None
    db.flush()
    return job


def cancel_job(db: Session, *, job_id: str) -> models.IngestionJob:
    job = db.get(models.IngestionJob, job_id)
    if job is None:
        raise ValueError("job not found")
    if job.state == "CANCELLED":
        return job
    if job.state not in {"PENDING", "RUNNING", "REVIEW"}:
        raise ValueError("job is not cancellable")
    active_stage = (
        ingestion_pipeline.IngestionStage.EXTRACT
        if job.stage == "EXTRACT"
        else ingestion_pipeline.IngestionStage.INDEX
    )
    ingestion_pipeline.record_stage_result(
        job,
        active_stage,
        ingestion_pipeline.StageResultStatus.CANCELLED,
        code="INGESTION_CANCELLED",
    )
    now = datetime.now(UTC)
    job.state = "CANCELLED"
    job.lock_owner = None
    job.locked_at = None
    job.next_attempt_at = None
    job.error_code = "INGESTION_CANCELLED"
    job.error_message = "cancelled by administrator"
    job.completed_at = now
    job.updated_at = now
    document = db.get(models.Document, job.document_id) if job.document_id else None
    if document is not None and document.status == "PROCESSING":
        document.status = "REVIEW"
        document.failure_code = "INGESTION_CANCELLED"
    db.flush()
    return job


def enqueue_reprocessing(
    db: Session, *, document_id: int, requested_by: int, version_id: int | None = None
) -> models.IngestionJob:
    """Create a new bounded reprocessing run for a document version.

    Reprocessing is a new idempotent work item, so a previous successful or
    failed job is never mutated in place and its audit/debug history remains.
    """
    del requested_by  # authorization/audit ownership is enforced by the caller
    document = db.get(models.Document, document_id)
    if document is None or document.lifecycle_state != "ACTIVE":
        raise ValueError("document is unavailable")
    version = db.get(models.DocVersion, version_id) if version_id else None
    if version is None:
        version = max(document.versions, key=lambda item: item.version_no, default=None)
    if version is None:
        raise ValueError("document has no version")
    job = job_repository.create_ingestion_job(
        db,
        job_id=str(uuid4()),
        idempotency_key=f"reprocess:{document_id}:{version.id}:{uuid4().hex}",
        stage_version="pipeline-v2",
        document_id=document.id,
        version_id=version.id,
    )
    job.stage = "EXTRACT"
    job.state = "PENDING"
    job.error_code = None
    job.error_message = None
    job.completed_at = None
    job.stage_results = "{}"
    job.degraded_stages = "[]"
    ingestion_pipeline.record_stage_result(
        job,
        ingestion_pipeline.IngestionStage.DEDUPLICATE,
        ingestion_pipeline.StageResultStatus.COMPLETED,
        metrics={"reprocessing": True},
    )
    document.status = "PROCESSING"
    document.failure_code = None
    db.flush()
    return job


__all__ = [
    "CHUNKER_VERSION", "EXTRACTOR_VERSION", "INDEX_VERSION",
    "enqueue_reprocessing", "run_claimed_job", "run_next_job",
    "mandatory_stages_complete", "retry_job", "cancel_job",
]
