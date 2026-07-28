import json
import logging

from app.observability import StructuredEventFormatter, event_logger, trace_span


def test_trace_span_is_redacted_and_has_stable_shape() -> None:
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = event_logger()
    logger.addHandler(handler)
    try:
        with trace_span("retrieval", "search"):
            pass
    finally:
        logger.removeHandler(handler)
    assert len(records) == 2
    started = json.loads(StructuredEventFormatter().format(records[0]))
    completed = json.loads(StructuredEventFormatter().format(records[1]))
    assert started["event"] == "trace.span.started"
    assert completed["event"] == "trace.span.completed"
    assert started["span_id"] == completed["span_id"]
    assert completed["component"] == "retrieval"
    assert "query" not in completed


def test_trace_span_records_error_without_exception_text() -> None:
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = event_logger()
    logger.addHandler(handler)
    try:
        try:
            with trace_span("sql", "query"):
                raise RuntimeError("secret SQL value")
        except RuntimeError:
            pass
    finally:
        logger.removeHandler(handler)
    completed = json.loads(StructuredEventFormatter().format(records[-1]))
    assert completed["outcome"] == "error"
    assert completed["error_type"] == "RuntimeError"
    assert "secret SQL value" not in json.dumps(completed)
