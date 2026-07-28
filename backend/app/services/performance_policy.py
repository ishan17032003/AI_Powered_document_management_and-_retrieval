"""Derive bounded worker/admission values from an approved measurement profile."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MeasuredCapacity:
    safe_concurrency: int
    p95_ms: float
    saturation_concurrency: int


@dataclass(frozen=True, slots=True)
class PerformanceLimits:
    worker_count: int
    queue_concurrency: int
    admission_limit: int
    stage_timeout_seconds: int


def derive_limits(measurement: MeasuredCapacity) -> PerformanceLimits:
    if measurement.safe_concurrency < 1 or measurement.saturation_concurrency <= measurement.safe_concurrency:
        raise ValueError("saturation must exceed safe concurrency")
    if measurement.p95_ms <= 0:
        raise ValueError("p95 latency must be positive")
    safe = min(measurement.safe_concurrency, measurement.saturation_concurrency - 1)
    return PerformanceLimits(
        worker_count=max(1, min(32, safe)),
        queue_concurrency=max(1, min(64, safe)),
        admission_limit=max(1, min(256, safe * 2)),
        stage_timeout_seconds=max(1, min(3600, int(max(10.0, measurement.p95_ms / 1000 * 20)))),
    )
