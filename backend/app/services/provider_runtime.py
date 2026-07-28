"""Bounded execution for synchronous provider calls made by web workers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

from ..observability import emit_event, trace_span
from ..utils.request_context import (
    bound_request_context,
    get_request_context,
    worker_context,
)


class ProviderRunner:
    """Run provider work in a capped daemon thread with no admission queue."""

    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def run(
        self,
        call: Callable[[], str | None],
        *,
        total_timeout_seconds: float,
    ) -> str | None:
        """Return a bounded result, or ``None`` on saturation, timeout, or failure."""

        work_context = worker_context(
            "provider",
            parent=get_request_context(None),
        )
        if total_timeout_seconds <= 0:
            emit_event(
                "worker.provider.rejected",
                level=logging.WARNING,
                context=work_context,
                component="worker",
                operation="provider_call",
                outcome="invalid_deadline",
            )
            return None
        if not self._slots.acquire(blocking=False):
            emit_event(
                "worker.provider.rejected",
                level=logging.WARNING,
                context=work_context,
                component="worker",
                operation="provider_call",
                outcome="saturated",
            )
            return None

        results: queue.Queue[str | None] = queue.Queue(maxsize=1)
        abandoned = threading.Event()
        started = time.monotonic()

        def invoke() -> None:
            with bound_request_context(work_context):
                emit_event(
                    "worker.provider.started",
                    context=work_context,
                    component="worker",
                    operation="provider_call",
                    outcome="accepted",
                )
                failure: BaseException | None = None
                result: str | None = None
                try:
                    try:
                        with trace_span("model", "approved_call", context=work_context):
                            result = call()
                    except Exception as exc:
                        failure = exc
                        result = None
                    if not abandoned.is_set():
                        try:
                            results.put_nowait(result)
                        except queue.Full:
                            pass
                finally:
                    # Admission capacity represents provider work, not log
                    # formatting. Release it as soon as the call has returned.
                    self._slots.release()
                    emit_event(
                        "worker.provider.completed",
                        level=logging.ERROR if failure is not None else logging.INFO,
                        context=work_context,
                        component="worker",
                        operation="provider_call",
                        outcome=(
                            "error"
                            if failure is not None
                            else ("success" if result else "empty")
                        ),
                        duration_ms=(time.monotonic() - started) * 1000,
                        error=failure,
                    )

        worker = threading.Thread(
            target=invoke,
            name="docvault-provider",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            self._slots.release()
            emit_event(
                "worker.provider.rejected",
                level=logging.ERROR,
                context=work_context,
                component="worker",
                operation="provider_call",
                outcome="start_failed",
                error=exc,
            )
            return None

        try:
            return results.get(timeout=total_timeout_seconds)
        except queue.Empty:
            abandoned.set()
            emit_event(
                "worker.provider.wait_completed",
                level=logging.WARNING,
                context=work_context,
                component="worker",
                operation="provider_call",
                outcome="timeout",
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return None
