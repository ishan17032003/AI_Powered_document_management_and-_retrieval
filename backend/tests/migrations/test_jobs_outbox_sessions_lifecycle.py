"""Upgrade, integrity, and recovery proofs for MIG-003."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
SOURCE_DATABASE = BACKEND_DIR / "docvault.db"
PREVIOUS_REVISION = "20260727_0003"
HEAD_REVISION = "20260727_0004"
NOW = "2026-07-27 12:00:00"

NEW_TABLES = (
    "auth_sessions",
    "auth_token_revocations",
    "ingestion_jobs",
    "outbox_events",
)


def _config(database: Path) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _revision(database: Path) -> str | None:
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _copy_source(destination: Path) -> dict[str, str]:
    before: dict[str, str] = {}
    for source in (SOURCE_DATABASE, Path(f"{SOURCE_DATABASE}-wal")):
        if not source.exists():
            continue
        before[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
        target = (
            destination if source == SOURCE_DATABASE else Path(f"{destination}-wal")
        )
        shutil.copy2(source, target)
    # The source is copied byte-for-byte above.  Checkpoint only the copy so
    # the source database and its WAL remain untouched.
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return before


@pytest.fixture
def migrated_database(tmp_path: Path) -> Path:
    database = tmp_path / "mig003.db"
    command.upgrade(_config(database), "head")
    assert _revision(database) == HEAD_REVISION
    return database


def test_upgrade_creates_bounded_durable_tables_and_fields(
    migrated_database: Path,
) -> None:
    engine = create_engine(f"sqlite:///{migrated_database}")
    try:
        inspector = inspect(engine)
        assert set(NEW_TABLES).issubset(inspector.get_table_names())
        assert {
            "lifecycle_state",
            "deleted_at",
            "failure_code",
        }.issubset({column["name"] for column in inspector.get_columns("documents")})
        assert {
            "storage_state",
            "extractor_version",
            "chunker_version",
        }.issubset({column["name"] for column in inspector.get_columns("doc_versions")})
        assert {
            "uq_doc_versions_document_version",
            "ix_doc_versions_storage_state",
        }.issubset({index["name"] for index in inspector.get_indexes("doc_versions")})
        assert {
            "ix_ingestion_jobs_state_available",
            "ix_outbox_events_state_available",
            "ix_auth_sessions_user_expiry",
            "ix_auth_token_revocations_expiry",
        }.issubset(
            {
                index["name"]
                for table in NEW_TABLES
                for index in inspector.get_indexes(table)
            }
        )
    finally:
        engine.dispose()


def test_existing_rows_receive_safe_lifecycle_defaults_without_data_rewrite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source-copy.db"
    source_hashes = _copy_source(database)
    config = _config(database)
    command.stamp(config, PREVIOUS_REVISION)
    with _connect(database) as connection:
        before_counts = (
            connection.execute("SELECT count(*) FROM documents").fetchone()[0],
            connection.execute("SELECT count(*) FROM doc_versions").fetchone()[0],
        )
    command.upgrade(config, HEAD_REVISION)
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT lifecycle_state, count(*) FROM documents GROUP BY lifecycle_state"
        ).fetchall() == [("ACTIVE", before_counts[0])]
        assert connection.execute(
            "SELECT storage_state, count(*) FROM doc_versions GROUP BY storage_state"
        ).fetchall() == [("AVAILABLE", before_counts[1])]
        assert connection.execute("SELECT count(*) FROM ingestion_jobs").fetchone() == (
            0,
        )
        assert connection.execute("SELECT count(*) FROM outbox_events").fetchone() == (
            0,
        )

    # Re-read source bytes after the migration; only the private copy changed.
    for source, digest in source_hashes.items():
        assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == digest


def test_integrity_checks_reject_invalid_states_duplicates_and_unbounded_values(
    migrated_database: Path,
) -> None:
    database = migrated_database
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO users(
                id, username, name, email, password_hash, status,
                mfa_enabled, created_at
            ) VALUES (1, 'mig003-user', 'Migration User', 'mig003@example.test',
                      'not-a-credential', 'active', 0, ?)
            """,
            (NOW,),
        )
        connection.execute(
            "INSERT INTO cabinets(id, name, parent_id) VALUES (1, 'Cabinet', NULL)"
        )
        connection.execute(
            "INSERT INTO folders(id, cabinet_id, parent_id, name) VALUES (1, 1, NULL, 'Folder')"
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, folder_id, title, class_id, class_confidence, content_hash,
                status, ocr_status, ocr_confidence, language, page_count,
                created_by, created_at, updated_at
            ) VALUES (1, 1, 'Document', NULL, NULL, :hash, 'READY', 'native',
                      NULL, 'eng', 1, 1, :now, :now)
            """,
            {"hash": "a" * 64, "now": NOW},
        )
        connection.execute(
            """
            INSERT INTO doc_versions(
                id, document_id, version_no, file_key, filename, content_type,
                size, checksum, ocr_text, created_by, created_at
            ) VALUES (1, 1, 1, 'objects/one', 'one.txt', 'text/plain',
                      3, :checksum, 'one', 1, :now)
            """,
            {"checksum": "b" * 64, "now": NOW},
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE documents SET lifecycle_state = 'INVALID' WHERE id = 1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE doc_versions SET checksum = :checksum WHERE id = 1",
                {"checksum": "c" * 64},
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO doc_versions(id, document_id, version_no, file_key, filename, content_type, size, checksum, ocr_text, created_by, created_at) "
                "VALUES (2, 1, 1, 'objects/two', 'two.txt', 'text/plain', 3, :checksum, '', 1, :now)",
                {"checksum": "d" * 64, "now": NOW},
            )
        connection.execute(
            """
            INSERT INTO ingestion_jobs(
                id, document_id, version_id, idempotency_key, created_at, updated_at
            ) VALUES ('11111111-1111-4111-8111-111111111111', 1, 1,
                      'job-one', :now, :now)
            """,
            {"now": NOW},
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingestion_jobs(id, idempotency_key, created_at, updated_at) "
                "VALUES ('22222222-2222-4222-8222-222222222222', 'job-one', :now, :now)",
                {"now": NOW},
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO outbox_events(id, aggregate_type, aggregate_id, event_type, payload, idempotency_key, state, available_at, created_at, updated_at) "
                "VALUES ('33333333-3333-4333-8333-333333333333', 'document', '1', 'created', '{}', 'event-one', 'INVALID', :now, :now, :now)",
                {"now": NOW},
            )
        connection.execute(
            """
            INSERT INTO auth_sessions(
                id, user_id, issued_at, expires_at, created_at
            ) VALUES ('session-one', 1, :now, '2026-08-01 12:00:00', :now)
            """,
            {"now": NOW},
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE auth_sessions SET token_version = -1 WHERE id = 'session-one'"
            )


def test_downgrade_removes_mig003_and_forward_upgrade_recreates_empty_state(
    migrated_database: Path,
) -> None:
    database = migrated_database
    config = _config(database)
    command.downgrade(config, PREVIOUS_REVISION)
    assert _revision(database) == PREVIOUS_REVISION
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert not set(NEW_TABLES).intersection(tables)
        assert "lifecycle_state" not in {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
    command.upgrade(config, HEAD_REVISION)
    with _connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM ingestion_jobs").fetchone() == (
            0,
        )
        assert connection.execute("SELECT count(*) FROM outbox_events").fetchone() == (
            0,
        )
