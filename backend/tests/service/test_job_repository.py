"""Bounded, transaction-neutral tests for JOB-001 repositories."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app import models
from app.repositories import job_repository


def test_ingestion_job_idempotency_and_bounds(db_session):
    key = f"job-{uuid4().hex}"
    first = job_repository.create_ingestion_job(
        db_session, job_id=uuid4().hex, idempotency_key=key
    )
    duplicate = job_repository.create_ingestion_job(
        db_session, job_id=uuid4().hex, idempotency_key=key, stage_version="other"
    )
    assert duplicate.id == first.id
    assert job_repository.get_ingestion_job(db_session, first.id) is first
    assert job_repository.list_ingestion_jobs(db_session, idempotency_key=key) == [first]
    with pytest.raises(ValueError):
        job_repository.list_ingestion_jobs(db_session, limit=0)
    with pytest.raises(ValueError):
        job_repository.list_ingestion_jobs(db_session, state="CLAIMED")


def test_outbox_payload_validation_and_idempotency(db_session):
    key = f"event-{uuid4().hex}"
    first = job_repository.create_outbox_event(
        db_session,
        event_id=uuid4().hex,
        aggregate_type="document",
        aggregate_id="1",
        event_type="document.created",
        payload={"document_id": 1},
        idempotency_key=key,
    )
    duplicate = job_repository.create_outbox_event(
        db_session,
        event_id=uuid4().hex,
        aggregate_type="document",
        aggregate_id="1",
        event_type="other",
        payload={"other": True},
        idempotency_key=key,
    )
    assert duplicate.id == first.id
    assert job_repository.get_outbox_event(db_session, first.id) is first
    assert job_repository.list_outbox_events(
        db_session, aggregate_type="document", aggregate_id="1"
    ) == [first]
    with pytest.raises(ValueError):
        job_repository.create_outbox_event(
            db_session,
            event_id=uuid4().hex,
            aggregate_type="document",
            aggregate_id="1",
            event_type="bad",
            payload="not json",
            idempotency_key=f"bad-{uuid4().hex}",
        )


def test_repository_does_not_commit_and_caller_can_rollback(db_session):
    job = job_repository.create_ingestion_job(
        db_session, job_id=uuid4().hex, idempotency_key=f"rollback-{uuid4().hex}"
    )
    db_session.rollback()
    assert db_session.get(models.IngestionJob, job.id) is None


def test_postgresql_profile_is_explicitly_supported(db_session, monkeypatch):
    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    monkeypatch.setattr(db_session, "get_bind", lambda: _Bind())
    assert job_repository._supported_dialect(db_session) == "postgresql"


def test_claim_heartbeat_retry_and_dead_letter_ownership(db_session):
    now = datetime.now(timezone.utc)
    job = job_repository.create_ingestion_job(
        db_session, job_id=uuid4().hex, idempotency_key=f"claim-{uuid4().hex}"
    )
    claimed = job_repository.claim_ingestion_job(db_session, owner="worker-a", now=now)
    assert claimed is not None and claimed.state == "RUNNING"
    assert job_repository.claim_ingestion_job(db_session, owner="worker-b", now=now) is None
    with pytest.raises(job_repository.JobRepositoryError, match="NOT_OWNED"):
        job_repository.heartbeat_ingestion_job(db_session, job_id=job.id, owner="worker-b")
    job_repository.heartbeat_ingestion_job(db_session, job_id=job.id, owner="worker-a", now=now)
    retried = job_repository.retry_ingestion_job(
        db_session, job_id=job.id, owner="worker-a", error_code="E", error_message="temporary", now=now
    )
    assert retried.state == "PENDING" and retried.next_attempt_at is not None
    claimed = job_repository.claim_ingestion_job(db_session, owner="worker-a", now=retried.next_attempt_at)
    assert claimed is not None
    dead = job_repository.dead_letter_ingestion_job(
        db_session, job_id=job.id, owner="worker-a", error_code="F", error_message="fatal", now=now
    )
    assert dead.state == "DEAD"


def test_admin_cancel_and_retry_job_state_transitions(db_session):
    job = job_repository.create_ingestion_job(
        db_session, job_id=uuid4().hex, idempotency_key=f"admin-{uuid4().hex}"
    )
    cancelled = job_repository.admin_cancel_ingestion_job(db_session, job_id=job.id)
    assert cancelled.state == "CANCELLED"
    with pytest.raises(job_repository.JobRepositoryError):
        job_repository.admin_cancel_ingestion_job(db_session, job_id=job.id)
    retried = job_repository.admin_retry_ingestion_job(db_session, job_id=job.id)
    assert retried.state == "PENDING"
