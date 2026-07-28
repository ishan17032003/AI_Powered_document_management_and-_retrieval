from app.services.performance_policy import MeasuredCapacity, derive_limits


def test_performance_limits_leave_headroom_below_saturation():
    limits = derive_limits(MeasuredCapacity(safe_concurrency=8, p95_ms=120, saturation_concurrency=12))
    assert limits.worker_count == 8
    assert limits.queue_concurrency == 8
    assert limits.admission_limit == 16
    assert limits.stage_timeout_seconds == 10
