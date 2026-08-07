"""Transaction-neutral persistence for ingestion jobs and outbox events.

This module deliberately only persists work items.  Claiming, leasing, retry,
and dead-letter transitions are implemented by the worker slice (JOB-002/003).
All methods use the caller-owned SQLAlchemy transaction and never commit or
roll it back.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import models

_MAX_LIMIT = 500
_JOB_STATES = frozenset({"PENDING", "RUNNING", "SUCCEEDED", "REVIEW", "FAILED", "DEAD", "CANCELLED"})
_OUTBOX_STATES = frozenset({"PENDING", "CLAIMED", "PROCESSED", "DEAD"})
_MAX_LEASE_SECONDS = 3_600
_MAX_RETRY_SECONDS = 86_400


class JobRepositoryError(RuntimeError):
    """Stable repository boundary failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _supported_dialect(db: Session) -> str:
    """Return the explicitly supported queue dialect.

    SQLite is intentionally a single-worker profile. PostgreSQL uses row
    locks for claims; callers must still use a normal transaction boundary.
    """
    dialect = db.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise JobRepositoryError("JOBS_DATABASE_PROFILE_UNSUPPORTED")
    return dialect


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{field} must be a non-empty string of at most {maximum} characters")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    return value


def _json_payload(payload: object) -> str:
    if isinstance(payload, str):
        raw = payload
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must contain valid JSON") from exc
    else:
        parsed = payload
        try:
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be JSON serializable") from exc
    if not isinstance(parsed, (dict, list)):
        raise ValueError("payload must be a JSON object or array")
    if not 2 <= len(raw.encode("utf-8")) <= 1_048_576:
        raise ValueError("payload must be between 2 bytes and 1 MiB")
    return raw


def create_ingestion_job(
    db: Session,
    *,
    job_id: str,
    idempotency_key: str,
    stage_version: str = "v1",
    document_id: int | None = None,
    version_id: int | None = None,
    next_attempt_at: datetime | None = None,
) -> models.IngestionJob:
    """Create or return the existing job for an idempotency key."""
    _supported_dialect(db)
    job_id = _text(job_id, field="job_id", maximum=36)
    idempotency_key = _text(idempotency_key, field="idempotency_key", maximum=200)
    stage_version = _text(stage_version, field="stage_version", maximum=40)
    existing = db.execute(
        select(models.IngestionJob).where(
            models.IngestionJob.idempotency_key == idempotency_key
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc)
    job = models.IngestionJob(
        id=job_id,
        idempotency_key=idempotency_key,
        stage_version=stage_version,
        document_id=document_id,
        version_id=version_id,
        next_attempt_at=next_attempt_at,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    return job


def get_ingestion_job(db: Session, job_id: str) -> models.IngestionJob | None:
    _supported_dialect(db)
    return db.get(models.IngestionJob, _text(job_id, field="job_id", maximum=36))


def admin_retry_ingestion_job(db: Session, *, job_id: str) -> models.IngestionJob:
    """Requeue a terminal retryable job under an administrator decision."""
    _supported_dialect(db)
    job = get_ingestion_job(db, job_id)
    if job is None or job.state not in {"FAILED", "DEAD", "REVIEW", "CANCELLED"}:
        raise JobRepositoryError("JOB_NOT_RETRYABLE")
    job.state = "PENDING"
    job.lock_owner = None
    job.locked_at = None
    job.next_attempt_at = datetime.now(timezone.utc)
    job.error_code = None
    job.error_message = None
    job.completed_at = None
    job.updated_at = datetime.now(timezone.utc)
    db.flush()
    return job


def admin_cancel_ingestion_job(db: Session, *, job_id: str) -> models.IngestionJob:
    """Cancel a pending/running job without deleting its audit trail."""
    _supported_dialect(db)
    job = get_ingestion_job(db, job_id)
    if job is None or job.state not in {"PENDING", "RUNNING"}:
        raise JobRepositoryError("JOB_NOT_CANCELLABLE")
    now = datetime.now(timezone.utc)
    job.state = "CANCELLED"
    job.lock_owner = None
    job.locked_at = None
    job.next_attempt_at = None
    job.error_code = "INGESTION_CANCELLED"
    job.error_message = "Cancelled by administrator"
    job.completed_at = now
    job.updated_at = now
    db.flush()
    return job


def list_ingestion_jobs(
    db: Session,
    *,
    state: str | None = None,
    idempotency_key: str | None = None,
    limit: int = 100,
) -> list[models.IngestionJob]:
    _supported_dialect(db)
    limit = _limit(limit)
    if state is not None and state not in _JOB_STATES:
        raise ValueError("invalid ingestion job state")
    query = select(models.IngestionJob)
    if state is not None:
        query = query.where(models.IngestionJob.state == state)
    if idempotency_key is not None:
        query = query.where(
            models.IngestionJob.idempotency_key
            == _text(idempotency_key, field="idempotency_key", maximum=200)
        )
    return list(
        db.execute(
            query.order_by(models.IngestionJob.created_at.desc(), models.IngestionJob.id)
            .limit(limit)
        ).scalars()
    )


def list_ingestion_jobs_after(
    db: Session,
    *,
    state: str | None = None,
    after_created_at=None,
    after_id: str | None = None,
    limit: int = 100,
) -> list[models.IngestionJob]:
    _supported_dialect(db)
    limit = _limit(limit)
    if state is not None and state not in _JOB_STATES:
        raise ValueError("invalid ingestion job state")
    query = select(models.IngestionJob)
    if state is not None:
        query = query.where(models.IngestionJob.state == state)
    if after_created_at is not None and after_id is not None:
        from sqlalchemy import or_
        query = query.where(
            or_(
                models.IngestionJob.created_at < after_created_at,
                (models.IngestionJob.created_at == after_created_at)
                & (models.IngestionJob.id > after_id),
            )
        )
    return list(
        db.execute(
            query.order_by(models.IngestionJob.created_at.desc(), models.IngestionJob.id.asc()).limit(limit)
        ).scalars()
    )


def _owner(value: object) -> str:
    return _text(value, field="owner", maximum=160)


def _lease(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_LEASE_SECONDS:
        raise ValueError(f"lease_seconds must be between 1 and {_MAX_LEASE_SECONDS}")
    return value


def claim_ingestion_job(
    db: Session,
    *,
    owner: str,
    now: datetime | None = None,
    lease_seconds: int = 60,
) -> models.IngestionJob | None:
    """Claim one available job; an active lease owned by another worker is skipped."""
    dialect = _supported_dialect(db)
    owner = _owner(owner)
    lease_seconds = _lease(lease_seconds)
    now = now or datetime.now(timezone.utc)
    expired_cutoff = now - timedelta(seconds=lease_seconds)
    available = (
        ((models.IngestionJob.state == "PENDING") &
         (models.IngestionJob.next_attempt_at.is_(None) |
          (models.IngestionJob.next_attempt_at <= now))) |
        ((models.IngestionJob.state == "RUNNING") &
         models.IngestionJob.locked_at.is_not(None) &
         (models.IngestionJob.locked_at <= expired_cutoff))
    )
    query = (
        select(models.IngestionJob)
        .where(available)
        .order_by(models.IngestionJob.created_at, models.IngestionJob.id)
        .limit(1)
    )
    if dialect == "postgresql":
        query = query.with_for_update(skip_locked=True)
    candidates = db.execute(query).scalars()
    job = next(iter(candidates), None)
    if job is None:
        return None
    result = db.execute(
        update(models.IngestionJob)
        .where(models.IngestionJob.id == job.id)
        .values(
            state="RUNNING",
            lock_owner=owner,
            locked_at=now,
            attempt_count=models.IngestionJob.attempt_count + 1,
            updated_at=now,
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        return None
    db.flush()
    db.expire(job)
    return job


def heartbeat_ingestion_job(
    db: Session, *, job_id: str, owner: str, now: datetime | None = None
) -> models.IngestionJob:
    _supported_dialect(db)
    now = now or datetime.now(timezone.utc)
    result = db.execute(
        update(models.IngestionJob)
        .where(
            models.IngestionJob.id == _text(job_id, field="job_id", maximum=36),
            models.IngestionJob.state == "RUNNING",
            models.IngestionJob.lock_owner == _owner(owner),
        )
        .values(locked_at=now, updated_at=now)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise JobRepositoryError("JOB_LEASE_NOT_OWNED")
    db.flush()
    return db.get(models.IngestionJob, job_id)  # type: ignore[return-value]


def retry_ingestion_job(
    db: Session,
    *,
    job_id: str,
    owner: str,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> models.IngestionJob:
    _supported_dialect(db)
    now = now or datetime.now(timezone.utc)
    attempt = db.execute(
        select(models.IngestionJob.attempt_count, models.IngestionJob.state, models.IngestionJob.lock_owner)
        .where(models.IngestionJob.id == _text(job_id, field="job_id", maximum=36))
    ).one_or_none()
    if attempt is None or attempt.state != "RUNNING" or attempt.lock_owner != _owner(owner):
        raise JobRepositoryError("JOB_LEASE_NOT_OWNED")
    delay = min(_MAX_RETRY_SECONDS, 2 ** min(int(attempt.attempt_count), 16))
    code = _text(error_code, field="error_code", maximum=80)
    message = _text(error_message, field="error_message", maximum=500)
    result = db.execute(
        update(models.IngestionJob)
        .where(models.IngestionJob.id == job_id, models.IngestionJob.lock_owner == owner)
        .values(state="PENDING", next_attempt_at=now + timedelta(seconds=delay), lock_owner=None, locked_at=None, error_code=code, error_message=message, updated_at=now)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise JobRepositoryError("JOB_LEASE_NOT_OWNED")
    db.flush()
    return db.get(models.IngestionJob, job_id)  # type: ignore[return-value]


def dead_letter_ingestion_job(
    db: Session, *, job_id: str, owner: str, error_code: str, error_message: str, now: datetime | None = None
) -> models.IngestionJob:
    _supported_dialect(db)
    now = now or datetime.now(timezone.utc)
    result = db.execute(
        update(models.IngestionJob).where(
            models.IngestionJob.id == _text(job_id, field="job_id", maximum=36),
            models.IngestionJob.state == "RUNNING",
            models.IngestionJob.lock_owner == _owner(owner),
        ).values(state="DEAD", lock_owner=None, locked_at=None, error_code=_text(error_code, field="error_code", maximum=80), error_message=_text(error_message, field="error_message", maximum=500), completed_at=now, updated_at=now)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise JobRepositoryError("JOB_LEASE_NOT_OWNED")
    db.flush()
    return db.get(models.IngestionJob, job_id)  # type: ignore[return-value]


def create_outbox_event(
    db: Session,
    *,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: object,
    idempotency_key: str,
    schema_version: int = 1,
    available_at: datetime | None = None,
) -> models.OutboxEvent:
    """Create or return an outbox event by idempotency key."""
    _supported_dialect(db)
    event_id = _text(event_id, field="event_id", maximum=36)
    aggregate_type = _text(aggregate_type, field="aggregate_type", maximum=40)
    aggregate_id = _text(aggregate_id, field="aggregate_id", maximum=80)
    event_type = _text(event_type, field="event_type", maximum=80)
    idempotency_key = _text(idempotency_key, field="idempotency_key", maximum=200)
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError("schema_version must be a positive integer")
    payload_text = _json_payload(payload)
    existing = db.execute(
        select(models.OutboxEvent).where(
            models.OutboxEvent.idempotency_key == idempotency_key
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc)
    event = models.OutboxEvent(
        id=event_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        schema_version=schema_version,
        payload=payload_text,
        idempotency_key=idempotency_key,
        available_at=available_at or now,
        created_at=now,
        updated_at=now,
    )
    db.add(event)
    db.flush()
    return event


def get_outbox_event(db: Session, event_id: str) -> models.OutboxEvent | None:
    _supported_dialect(db)
    return db.get(models.OutboxEvent, _text(event_id, field="event_id", maximum=36))


def list_outbox_events(
    db: Session,
    *,
    state: str | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    limit: int = 100,
) -> list[models.OutboxEvent]:
    _supported_dialect(db)
    limit = _limit(limit)
    if state is not None and state not in _OUTBOX_STATES:
        raise ValueError("invalid outbox event state")
    query = select(models.OutboxEvent)
    if state is not None:
        query = query.where(models.OutboxEvent.state == state)
    if aggregate_type is not None:
        query = query.where(
            models.OutboxEvent.aggregate_type
            == _text(aggregate_type, field="aggregate_type", maximum=40)
        )
    if aggregate_id is not None:
        query = query.where(
            models.OutboxEvent.aggregate_id
            == _text(aggregate_id, field="aggregate_id", maximum=80)
        )
    return list(
        db.execute(
            query.order_by(models.OutboxEvent.created_at.desc(), models.OutboxEvent.id)
            .limit(limit)
        ).scalars()
    )
