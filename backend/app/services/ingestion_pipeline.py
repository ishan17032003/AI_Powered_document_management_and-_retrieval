"""Durable, idempotent ingestion stage transitions.

The upload request creates a PENDING job at EXTRACT. Workers can advance one
stage at a time; repeated acknowledgements are no-ops, while stale or illegal
transitions fail closed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.orm import Session

from .. import models


class IngestionStage(StrEnum):
    EXTRACT = "EXTRACTING"
    CLASSIFY = "CLASSIFYING"
    DEDUPLICATE = "DEDUPLICATING"
    CHUNK = "CHUNKING"
    INDEX = "INDEXING"


class StageResultStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    REVIEW = "review"
    FAILED = "failed"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    CANCELLED = "cancelled"


MANDATORY_STAGES: tuple[IngestionStage, ...] = (
    IngestionStage.EXTRACT,
    IngestionStage.CLASSIFY,
    IngestionStage.DEDUPLICATE,
    IngestionStage.CHUNK,
    IngestionStage.INDEX,
)


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    ready: bool
    review_required: bool
    missing_stages: tuple[str, ...]
    degraded_stages: tuple[str, ...]


class IngestionStageError(ValueError):
    """Raised when a worker attempts an invalid stage transition."""


def ingestion_stage_plan() -> tuple[IngestionStage, ...]:
    return MANDATORY_STAGES


def stage_results(job: models.IngestionJob) -> dict[str, dict[str, object]]:
    try:
        parsed = json.loads(job.stage_results or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(name): dict(result)
        for name, result in parsed.items()
        if isinstance(name, str) and isinstance(result, dict)
    }


def degraded_stages(job: models.IngestionJob) -> list[str]:
    try:
        parsed = json.loads(job.degraded_stages or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return sorted(
        {
            value
            for value in parsed
            if isinstance(value, str) and 1 <= len(value) <= 40
        }
    )


def _safe_metrics(
    values: Mapping[str, bool | float | int | str] | None,
) -> dict[str, bool | float | int | str]:
    safe: dict[str, bool | float | int | str] = {}
    for key, value in (values or {}).items():
        if (
            isinstance(key, str)
            and 1 <= len(key) <= 40
            and isinstance(value, (bool, float, int, str))
            and (not isinstance(value, str) or len(value) <= 80)
        ):
            safe[key] = value
    return safe


def record_stage_result(
    job: models.IngestionJob,
    stage: IngestionStage,
    status: StageResultStatus,
    *,
    code: str | None = None,
    metrics: Mapping[str, bool | float | int | str] | None = None,
) -> None:
    results = stage_results(job)
    result: dict[str, object] = {
        "status": status.value,
        "required": stage in MANDATORY_STAGES,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if code is not None and 1 <= len(code) <= 80:
        result["code"] = code
    safe_metrics = _safe_metrics(metrics)
    if safe_metrics:
        result["metrics"] = safe_metrics
    results[stage.value] = result
    job.stage_results = json.dumps(
        results,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    degraded = set(degraded_stages(job))
    if status is StageResultStatus.DEGRADED:
        degraded.add(stage.value)
    else:
        degraded.discard(stage.value)
    job.degraded_stages = json.dumps(sorted(degraded), separators=(",", ":"))


def record_optional_stage(
    job: models.IngestionJob,
    name: str,
    status: StageResultStatus,
    *,
    code: str | None = None,
    metrics: Mapping[str, bool | float | int | str] | None = None,
) -> None:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,39}", name):
        raise IngestionStageError("invalid optional ingestion stage")
    results = stage_results(job)
    result: dict[str, object] = {
        "status": status.value,
        "required": False,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if code is not None and 1 <= len(code) <= 80:
        result["code"] = code
    safe_metrics = _safe_metrics(metrics)
    if safe_metrics:
        result["metrics"] = safe_metrics
    results[name] = result
    job.stage_results = json.dumps(
        results,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    degraded = set(degraded_stages(job))
    if status is StageResultStatus.DEGRADED:
        degraded.add(name)
    else:
        degraded.discard(name)
    job.degraded_stages = json.dumps(sorted(degraded), separators=(",", ":"))


def evaluate_readiness(job: models.IngestionJob) -> ReadinessDecision:
    results = stage_results(job)
    missing: list[str] = []
    review_required = False
    for stage in MANDATORY_STAGES:
        status = results.get(stage.value, {}).get("status")
        if status != StageResultStatus.COMPLETED.value:
            missing.append(stage.value)
        if status in {
            StageResultStatus.REVIEW.value,
            StageResultStatus.DEGRADED.value,
        }:
            review_required = True
    degraded = tuple(degraded_stages(job))
    return ReadinessDecision(
        ready=not missing and not review_required and not degraded,
        review_required=review_required or bool(degraded),
        missing_stages=tuple(missing),
        degraded_stages=degraded,
    )


def clear_retry_results(job: models.IngestionJob) -> None:
    results = stage_results(job)
    reset = (
        MANDATORY_STAGES
        if job.stage == "EXTRACT"
        else (IngestionStage.INDEX,)
    )
    for stage in reset:
        results.pop(stage.value, None)
    if IngestionStage.INDEX in reset:
        results.pop("VECTOR_INDEXING", None)
    job.stage_results = json.dumps(
        results,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    degraded = set(degraded_stages(job))
    degraded.difference_update(stage.value for stage in reset)
    if IngestionStage.INDEX in reset:
        degraded.discard("VECTOR_INDEXING")
    job.degraded_stages = json.dumps(sorted(degraded), separators=(",", ":"))


def advance_stage(
    db: Session,
    job: models.IngestionJob,
    *,
    expected: str,
    next_stage: str,
) -> models.IngestionJob:
    """Advance EXTRACT→INDEX exactly once, without committing the caller txn."""

    if expected not in {"EXTRACT", "INDEX"} or next_stage not in {"EXTRACT", "INDEX"}:
        raise IngestionStageError("invalid ingestion stage")
    if job.state not in {"PENDING", "RUNNING"}:
        raise IngestionStageError("job is not executable")
    if job.stage == next_stage:
        return job  # idempotent worker retry
    if job.stage != expected or (expected, next_stage) != ("EXTRACT", "INDEX"):
        raise IngestionStageError("invalid ingestion stage transition")
    job.stage = next_stage
    job.updated_at = datetime.now(UTC)
    db.flush()
    return job


def complete_stage(
    db: Session,
    job: models.IngestionJob,
    *,
    expected: str,
) -> models.IngestionJob:
    """Mark a job successful after INDEX; repeated completion is idempotent."""

    if job.state == "SUCCEEDED":
        return job
    if job.state not in {"PENDING", "RUNNING"} or job.stage != expected or expected != "INDEX":
        raise IngestionStageError("job is not ready for completion")
    now = datetime.now(UTC)
    job.state = "SUCCEEDED"
    job.completed_at = now
    job.updated_at = now
    db.flush()
    return job


__all__ = [
    "IngestionStage",
    "IngestionStageError",
    "MANDATORY_STAGES",
    "ReadinessDecision",
    "StageResultStatus",
    "advance_stage",
    "clear_retry_results",
    "complete_stage",
    "degraded_stages",
    "evaluate_readiness",
    "ingestion_stage_plan",
    "record_optional_stage",
    "record_stage_result",
    "stage_results",
]
