"""AUDIT-003: database-level append-only audit guarantees."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _config(database: Path) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def test_audit_rows_are_append_only(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    command.upgrade(_config(database), "head")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO audit_log (actor_name, action, object_type, object_id, ip, user_agent, details, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ("admin", "TEST", "document", "1", "", "", "{}"),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_append_only"):
            connection.execute("UPDATE audit_log SET action = 'TAMPERED' WHERE id = 1")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_append_only"):
            connection.execute("DELETE FROM audit_log WHERE id = 1")
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (1,)
    finally:
        connection.close()


def test_audit_insert_remains_allowed(tmp_path: Path) -> None:
    database = tmp_path / "audit-insert.db"
    command.upgrade(_config(database), "head")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO audit_log (actor_name, action, object_type, object_id, ip, user_agent, details, timestamp) "
            "VALUES ('system', 'INSERT', 'job', '1', '', '', '{}', CURRENT_TIMESTAMP)"
        )
        connection.commit()
        assert connection.execute("SELECT action FROM audit_log").fetchone() == ("INSERT",)
    finally:
        connection.close()
