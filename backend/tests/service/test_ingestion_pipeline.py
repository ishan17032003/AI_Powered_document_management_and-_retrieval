from uuid import uuid4

import pytest

from app.repositories import job_repository
from app.services.ingestion_pipeline import (
    IngestionStageError,
    advance_stage,
    complete_stage,
)


def test_ingestion_stage_transitions_are_ordered_and_idempotent(db_session):
    job = job_repository.create_ingestion_job(
        db_session, job_id=str(uuid4()), idempotency_key=f"stage-{uuid4()}"
    )
    assert job.stage == "EXTRACT"
    advance_stage(db_session, job, expected="EXTRACT", next_stage="INDEX")
    advance_stage(db_session, job, expected="EXTRACT", next_stage="INDEX")
    complete_stage(db_session, job, expected="INDEX")
    complete_stage(db_session, job, expected="INDEX")
    assert job.state == "SUCCEEDED"


def test_ingestion_stage_rejects_skip_and_post_completion(db_session):
    job = job_repository.create_ingestion_job(
        db_session, job_id=str(uuid4()), idempotency_key=f"stage-{uuid4()}"
    )
    with pytest.raises(IngestionStageError):
        complete_stage(db_session, job, expected="EXTRACT")
    advance_stage(db_session, job, expected="EXTRACT", next_stage="INDEX")
    complete_stage(db_session, job, expected="INDEX")
    with pytest.raises(IngestionStageError):
        advance_stage(db_session, job, expected="INDEX", next_stage="EXTRACT")
