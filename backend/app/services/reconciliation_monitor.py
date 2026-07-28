"""Bounded reconciliation and stale-job monitoring metrics.

The monitor is intentionally read-only.  It exposes a small, stable snapshot
that operators and alerting systems can poll without running ad-hoc database
queries.  Reconciliation findings may be supplied by a future adapter; job
health is always derived from the durable ingestion queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models

_MAX_STALE_SECONDS = 7 * 24 * 60 * 60


def _safe_snapshot(value: Mapping[str, object] | None) -> dict[str, object]:
    """Copy bounded scalar metric snapshots without accepting raw content."""
    if value is None:
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            continue
        if type(item) in (int, float, bool) and (not isinstance(item, float) or item == item):
            result[key] = item
    return result


def collect_release_metrics(
    db: Session,
    *,
    stale_after_seconds: int = 3600,
    findings: Mapping[str, int] | None = None,
    latency: Mapping[str, object] | None = None,
    errors: Mapping[str, object] | None = None,
    auth_failures: Mapping[str, object] | None = None,
    stage_timing: Mapping[str, object] | None = None,
    lane_quality: Mapping[str, object] | None = None,
    provider_health: Mapping[str, object] | None = None,
    resources: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the stable release-critical metric surface.

    Runtime components may supply bounded scalar snapshots; absent optional
    collectors remain empty rather than inventing health. Durable queue and
    reconciliation metrics are always collected from the database.
    """
    base = collect_metrics(
        db, stale_after_seconds=stale_after_seconds, findings=findings
    )
    return {
        "generated_at": base["generated_at"],
        "latency": _safe_snapshot(latency),
        "errors": _safe_snapshot(errors),
        "auth_failures": _safe_snapshot(auth_failures),
        "queues_retries_dead_letters": base["jobs"],
        "stage_timing": _safe_snapshot(stage_timing),
        "source_index_acl_drift": base["drift"],
        "lane_quality_freshness": _safe_snapshot(lane_quality),
        "disk_model_memory_vram": _safe_snapshot(resources),
        "provider_health": _safe_snapshot(provider_health),
        "alerts": base["alerts"],
        "healthy": base["healthy"],
    }


def _bounded_seconds(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_STALE_SECONDS:
        raise ValueError("stale_after_seconds must be between 1 and 604800")
    return value


def collect_metrics(
    db: Session,
    *,
    stale_after_seconds: int = 3600,
    findings: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Return a bounded monitoring snapshot without mutating the database."""
    stale_after_seconds = _bounded_seconds(stale_after_seconds)
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=stale_after_seconds)

    rows = db.execute(
        select(models.IngestionJob.state, func.count())
        .group_by(models.IngestionJob.state)
    ).all()
    states = {str(state): int(count) for state, count in rows}
    stale_running = int(
        db.scalar(
            select(func.count())
            .select_from(models.IngestionJob)
            .where(
                models.IngestionJob.state == "RUNNING",
                models.IngestionJob.locked_at.is_not(None),
                models.IngestionJob.locked_at < cutoff,
            )
        )
        or 0
    )
    drift = {
        str(key): int(value)
        for key, value in (findings or {}).items()
        if isinstance(key, str)
        and len(key) <= 64
        and type(value) is int
        and 0 <= value <= 2_147_483_647
    }
    drift_total = sum(drift.values())
    alert_names: list[str] = []
    if stale_running:
        alert_names.append("stale_processing_jobs")
    if states.get("FAILED", 0) or states.get("DEAD", 0):
        alert_names.append("failed_processing_jobs")
    if drift_total:
        alert_names.append("reconciliation_drift")
    return {
        "generated_at": now,
        "stale_after_seconds": stale_after_seconds,
        "jobs": {
            "by_state": states,
            "stale_running": stale_running,
            "failed_or_dead": states.get("FAILED", 0) + states.get("DEAD", 0),
        },
        "drift": {"by_type": drift, "total": drift_total},
        "alerts": alert_names,
        "healthy": not alert_names,
    }
