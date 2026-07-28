from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import models
from app.services.reconciliation_monitor import collect_metrics, collect_release_metrics


def _job(db_session, *, state: str, locked_at=None):
    job = models.IngestionJob(
        id=uuid4().hex,
        state=state,
        idempotency_key=uuid4().hex,
        stage_version="v1",
        stage="EXTRACT",
        locked_at=locked_at,
    )
    db_session.add(job)
    db_session.flush()
    return job


def test_metrics_report_stale_jobs_and_drift(db_session):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _job(db_session, state="RUNNING", locked_at=old)
    _job(db_session, state="FAILED")
    metrics = collect_metrics(
        db_session,
        stale_after_seconds=3600,
        findings={"missing_objects": 2, "malformed": -1, "orphaned": 1},
    )
    assert metrics["jobs"]["stale_running"] == 1
    assert metrics["jobs"]["failed_or_dead"] == 1
    assert metrics["drift"] == {"by_type": {"missing_objects": 2, "orphaned": 1}, "total": 3}
    assert metrics["healthy"] is False
    assert "stale_processing_jobs" in metrics["alerts"]


def test_metrics_empty_snapshot_is_healthy(db_session):
    metrics = collect_metrics(db_session, stale_after_seconds=60)
    assert metrics["healthy"] is True
    assert metrics["alerts"] == []
    assert metrics["jobs"]["by_state"] == {}


@pytest.mark.parametrize("value", [0, -1, 604801, True])
def test_metrics_bound_stale_window(db_session, value):
    with pytest.raises(ValueError):
        collect_metrics(db_session, stale_after_seconds=value)


def test_release_metrics_exposes_bounded_critical_surfaces(db_session):
    metrics = collect_release_metrics(
        db_session,
        latency={"p95_ms": 42.5, "route": "must-be-dropped"},
        provider_health={"ollama_ready": True},
        resources={"vram_mb": 512},
    )

    assert metrics["latency"] == {"p95_ms": 42.5}
    assert metrics["queues_retries_dead_letters"]["by_state"] == {}
    assert metrics["source_index_acl_drift"]["total"] == 0
    assert metrics["provider_health"] == {"ollama_ready": True}
    assert metrics["disk_model_memory_vram"] == {"vram_mb": 512}
    assert "auth_failures" in metrics and "stage_timing" in metrics
