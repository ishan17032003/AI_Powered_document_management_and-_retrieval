"""Database engine and session lifecycle; Alembic exclusively owns schema."""

from __future__ import annotations

from collections.abc import Generator
from time import monotonic
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .observability import emit_event
from .runtime import settings

_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    """Enable foreign keys + WAL for the local SQLite backend."""
    if settings.database_url.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        # Wait up to 10s for a lock instead of failing immediately — a long-running
        # answer request can hold a connection open while other requests write.
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


@event.listens_for(engine, "before_cursor_execute")
def _trace_sql_start(conn, cursor, statement, parameters, context, executemany):
    del cursor, statement, parameters, executemany
    span_id = f"span:{uuid4().hex}"
    conn.info["docvault_sql_started"] = (monotonic(), span_id)
    emit_event("trace.span.started", component="sql", operation="query", outcome="started", span_id=span_id)


@event.listens_for(engine, "after_cursor_execute")
def _trace_sql_complete(conn, cursor, statement, parameters, context, executemany):
    del cursor, statement, parameters, context, executemany
    started, span_id = conn.info.pop("docvault_sql_started", (None, None))
    emit_event(
        "trace.span.completed",
        component="sql",
        operation="query",
        outcome="success",
        duration_ms=(monotonic() - started) * 1000 if started else None,
        span_id=span_id,
    )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
