"""Minimal structured, redacted operational event logging.

The API intentionally accepts only a small allowlist of scalar fields. It does
not expose a generic ``extra`` mapping, message interpolation, or exception
tracebacks, so request content and credentials cannot be logged accidentally.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from .utils.request_context import (
    RequestContext,
    get_request_context,
    normalize_external_id,
    safe_document_identifier,
)


def sensitive_query_telemetry(query: object) -> dict[str, object]:
    """Return policy-safe query telemetry without retaining query content.

    Search/RAG prompts can contain protected personal or business data.  The
    approved telemetry shape is a bounded length and a keyed, one-way digest;
    callers must never pass the raw query to a logger or metric label.
    """
    if not isinstance(query, str):
        return {"query_length": 0, "query_hash": None}
    encoded = query.encode("utf-8")
    from .config import settings

    key = settings.secret_key.encode("utf-8")
    digest = hmac.new(key, encoded, hashlib.sha256).hexdigest() if key else None
    return {
        "query_length": min(len(encoded), 1_000_000),
        "query_hash": f"hmac-sha256:{digest}" if digest else None,
    }

EVENT_SCHEMA_VERSION: Final = "docvault.operational-event.v1"
LOGGER_NAME: Final = "docvault.events"

_EVENT_ATTRIBUTE = "docvault_event"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_ACTOR_PATTERN = re.compile(r"user:[1-9][0-9]{0,18}")
_JOB_PATTERN = re.compile(r"job:[A-Za-z0-9_.:-]{1,59}")
_ROUTE_PATTERN = re.compile(r"/[A-Za-z0-9_./{}:-]{0,199}")
_METHOD_PATTERN = re.compile(r"[A-Z]{1,12}")
_KNOWN_EVENTS: Final = {
    "auth.login.throttled",
    "authorization.policy_revision.bumped",
    "audit.write.completed",
    "extraction.engine.initialized",
    "http.request.completed",
    "http.request.started",
    "logging.invalid_event",
    "worker.model_warm.completed",
    "worker.model_warm.rejected",
    "worker.model_warm.started",
    "worker.provider.completed",
    "worker.provider.rejected",
    "worker.provider.started",
    "worker.provider.wait_completed",
    "trace.span.started",
    "trace.span.completed",
}

_BASE_FIELDS: Final = (
    "actor_id",
    "component",
    "correlation_id",
    "count",
    "document_id",
    "duration_ms",
    "error_type",
    "event",
    "job_id",
    "level",
    "method",
    "operation",
    "outcome",
    "request_id",
    "route",
    "schema",
    "status_code",
    "span_id",
    "parent_span_id",
    "timestamp",
)


def _safe_token(value: object) -> str | None:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        return None
    if normalize_external_id(value) is None:
        return None
    return value


def _safe_event(value: object) -> str:
    return (
        value
        if isinstance(value, str) and value in _KNOWN_EVENTS
        else "logging.invalid_event"
    )


def _safe_actor(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and _ACTOR_PATTERN.fullmatch(value) is not None
        else None
    )


def _safe_job(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and _JOB_PATTERN.fullmatch(value) is not None
        else None
    )


def _safe_route(value: object) -> str | None:
    if not isinstance(value, str) or _ROUTE_PATTERN.fullmatch(value) is None:
        return None
    return value


def _safe_method(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.upper()
    return normalized if _METHOD_PATTERN.fullmatch(normalized) else None


def _safe_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def _safe_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 2_147_483_647 else None


def _safe_duration(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 86_400_000:
        return None
    return round(numeric, 3)


def _safe_error_type(error: BaseException | None) -> str | None:
    if error is None:
        return None
    return _safe_token(type(error).__name__) or "Exception"


def _sanitize_record_fields(supplied: object) -> dict[str, object]:
    event = supplied if isinstance(supplied, dict) else {}
    return {
        "actor_id": _safe_actor(event.get("actor_id")),
        "component": _safe_token(event.get("component")),
        "correlation_id": normalize_external_id(event.get("correlation_id")),
        "count": _safe_count(event.get("count")),
        "document_id": safe_document_identifier(event.get("document_id")),
        "duration_ms": _safe_duration(event.get("duration_ms")),
        "error_type": _safe_token(event.get("error_type")),
        "event": _safe_event(event.get("event")),
        "job_id": _safe_job(event.get("job_id")),
        "method": _safe_method(event.get("method")),
        "operation": _safe_token(event.get("operation")),
        "outcome": _safe_token(event.get("outcome")),
        "request_id": normalize_external_id(event.get("request_id")),
        "route": _safe_route(event.get("route")),
        "status_code": _safe_status(event.get("status_code")),
        "span_id": _safe_token(event.get("span_id")),
        "parent_span_id": _safe_token(event.get("parent_span_id")),
    }


class StructuredEventFormatter(logging.Formatter):
    """Render one stable JSON object per operational event."""

    def format(self, record: logging.LogRecord) -> str:
        supplied = getattr(record, _EVENT_ATTRIBUTE, {})
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(
            timespec="milliseconds"
        )
        payload = dict.fromkeys(_BASE_FIELDS)
        payload.update(_sanitize_record_fields(supplied))
        payload["level"] = record.levelname.lower()
        payload["schema"] = EVENT_SCHEMA_VERSION
        payload["timestamp"] = timestamp.replace("+00:00", "Z")
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


class _StructuredEventHandler(logging.StreamHandler):
    _docvault_structured_handler = True


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    # Alembic and process-server logging configuration may disable pre-existing
    # named loggers. Operational events must remain available after either one.
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(
        getattr(handler, "_docvault_structured_handler", False)
        for handler in logger.handlers
    ):
        handler = _StructuredEventHandler()
        handler.setFormatter(StructuredEventFormatter())
        logger.addHandler(handler)
    return logger


_logger = _configure_logger()


def event_logger() -> logging.Logger:
    """Expose the dedicated logger for deployment configuration and tests."""

    return _configure_logger()


def emit_event(
    event: str,
    *,
    level: int = logging.INFO,
    context: RequestContext | None = None,
    component: str | None = None,
    operation: str | None = None,
    outcome: str | None = None,
    document_id: object = None,
    status_code: int | None = None,
    duration_ms: float | None = None,
    count: int | None = None,
    method: str | None = None,
    route: str | None = None,
    error: BaseException | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> None:
    """Emit a schema-stable event without accepting protected free-form data."""

    request_context = context or get_request_context(None)
    safe_document_id = safe_document_identifier(document_id)
    if safe_document_id is None:
        safe_document_id = safe_document_identifier(request_context.document_id)
    fields = {
        "actor_id": _safe_actor(request_context.actor_id),
        "component": _safe_token(component),
        "correlation_id": normalize_external_id(request_context.correlation_id),
        "count": _safe_count(count),
        "document_id": safe_document_id,
        "duration_ms": _safe_duration(duration_ms),
        "error_type": _safe_error_type(error),
        "event": _safe_event(event),
        "job_id": _safe_job(request_context.job_id),
        "method": _safe_method(method),
        "operation": _safe_token(operation),
        "outcome": _safe_token(outcome),
        "request_id": normalize_external_id(request_context.request_id),
        "route": _safe_route(route),
        "status_code": _safe_status(status_code),
        "span_id": _safe_token(span_id),
        "parent_span_id": _safe_token(parent_span_id),
    }
    event_logger().log(level, "", extra={_EVENT_ATTRIBUTE: fields})


@contextmanager
def trace_span(
    component: str,
    operation: str,
    *,
    context: RequestContext | None = None,
    document_id: object = None,
):
    """Emit a redacted, OpenTelemetry-shaped span without protected attributes.

    Deployments may bridge these stable events to OpenTelemetry.  Span IDs are
    opaque and all attributes pass through the same allowlist as operational
    events; request bodies, SQL text, paths, prompts, and model output never
    enter telemetry.
    """
    span_id = f"span:{uuid4().hex}"
    started = time.monotonic()
    emit_event(
        "trace.span.started",
        context=context,
        component=component,
        operation=operation,
        outcome="started",
        document_id=document_id,
        span_id=span_id,
    )
    try:
        yield span_id
    except Exception as exc:
        emit_event(
            "trace.span.completed",
            level=logging.ERROR,
            context=context,
            component=component,
            operation=operation,
            outcome="error",
            duration_ms=(time.monotonic() - started) * 1000,
            document_id=document_id,
            error=exc,
            span_id=span_id,
        )
        raise
    else:
        emit_event(
            "trace.span.completed",
            context=context,
            component=component,
            operation=operation,
            outcome="success",
            duration_ms=(time.monotonic() - started) * 1000,
            document_id=document_id,
            span_id=span_id,
        )


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "StructuredEventFormatter",
    "emit_event",
    "event_logger",
    "trace_span",
]
