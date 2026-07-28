"""Deterministic proof for the initial Alembic schema baseline."""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app import models  # noqa: F401
from app.model_base import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
SOURCE_DATABASE = BACKEND_DIR / "docvault.db"
BASELINE_REVISION = "20260727_0001"
HEAD_REVISION = "20260727_0004"
LEGACY_TABLES = (
    "assignments",
    "audit_log",
    "cabinets",
    "doc_classes",
    "doc_metadata",
    "doc_versions",
    "documents",
    "dup_groups",
    "dup_members",
    "folders",
    "permissions",
    "role_permissions",
    "roles",
    "users",
)
ACL_TABLES = (
    "access_rules",
    "authorization_policy_state",
    "group_memberships",
    "groups",
)
LIFECYCLE_TABLES = (
    "auth_sessions",
    "auth_token_revocations",
    "ingestion_jobs",
    "outbox_events",
)
CORE_TABLES = (*LEGACY_TABLES, *ACL_TABLES, *LIFECYCLE_TABLES)
FTS_SQL = """
CREATE VIRTUAL TABLE doc_fts USING fts5(
    document_id UNINDEXED,
    title,
    content,
    tokenize='porter unicode61'
)
"""


def _alembic_config(database: Path) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _current_revision(database: Path) -> str | None:
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _copy_current_snapshot(destination: Path) -> None:
    """Copy DB/WAL/SHM bytes to /tmp without opening the protected source DB."""
    assert SOURCE_DATABASE.is_file(), "validated checked-in snapshot is missing"
    shutil.copy2(SOURCE_DATABASE, destination)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{SOURCE_DATABASE}{suffix}")
        if sidecar.is_file():
            shutil.copy2(sidecar, Path(f"{destination}{suffix}"))

    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _normalized_schema_sql(database: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(database) as connection:
        objects = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index')
            ORDER BY type, name
            """
        ).fetchall()
    return tuple(
        (object_type, name, re.sub(r"\s+", " ", sql).strip())
        for object_type, name, sql in objects
        if sql is not None
        and name != "alembic_version"
        and not name.startswith("sqlite_")
        and not name.startswith("doc_fts_")
    )


def _schema_signature(database: Path) -> dict[str, Any]:
    """Capture logical schema while excluding Alembic and FTS implementation tables."""
    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        discovered = {
            table
            for table in inspector.get_table_names()
            if table != "alembic_version" and not table.startswith("doc_fts_")
        }
        assert discovered == {*CORE_TABLES, "doc_fts"}

        tables: dict[str, Any] = {}
        for table in CORE_TABLES:
            columns = tuple(
                (
                    column["name"],
                    str(column["type"]).upper(),
                    column["nullable"],
                    column["default"],
                )
                for column in inspector.get_columns(table)
            )
            primary_key = tuple(
                inspector.get_pk_constraint(table).get("constrained_columns") or ()
            )
            foreign_keys = tuple(
                sorted(
                    (
                        tuple(item["constrained_columns"]),
                        item["referred_table"],
                        tuple(item["referred_columns"]),
                        tuple(sorted((item.get("options") or {}).items())),
                    )
                    for item in inspector.get_foreign_keys(table)
                )
            )
            unique_constraints = tuple(
                sorted(
                    tuple(item["column_names"])
                    for item in inspector.get_unique_constraints(table)
                )
            )
            indexes = tuple(
                sorted(
                    (
                        item["name"],
                        tuple(item["column_names"]),
                        bool(item["unique"]),
                    )
                    for item in inspector.get_indexes(table)
                )
            )
            tables[table] = {
                "columns": columns,
                "primary_key": primary_key,
                "foreign_keys": foreign_keys,
                "unique_constraints": unique_constraints,
                "indexes": indexes,
            }
        return {
            "tables": tables,
            "normalized_sql": _normalized_schema_sql(database),
        }
    finally:
        engine.dispose()


def _row_counts(
    database: Path,
    *,
    tables: tuple[str, ...] = (*CORE_TABLES, "doc_fts"),
) -> dict[str, int]:
    engine = create_engine(f"sqlite:///{database}")
    metadata = sa.MetaData()
    try:
        with engine.connect() as connection:
            return {
                table_name: connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        sa.Table(table_name, metadata, autoload_with=engine)
                    )
                )
                or 0
                for table_name in tables
            }
    finally:
        engine.dispose()


def test_empty_model_and_current_snapshot_share_baseline_schema(
    tmp_path: Path,
) -> None:
    migrated_database = tmp_path / "migrated-empty.db"
    model_database = tmp_path / "model-empty.db"
    snapshot_database = tmp_path / "current-snapshot-copy.db"

    migrated_config = _alembic_config(migrated_database)
    command.upgrade(migrated_config, "head")
    assert _current_revision(migrated_database) == HEAD_REVISION
    command.check(migrated_config)

    model_engine = create_engine(f"sqlite:///{model_database}")
    try:
        Base.metadata.create_all(model_engine)
        with model_engine.begin() as connection:
            connection.execute(sa.text(FTS_SQL))
    finally:
        model_engine.dispose()

    _copy_current_snapshot(snapshot_database)
    migrated_signature = _schema_signature(migrated_database)
    assert _schema_signature(model_database) == migrated_signature

    before_counts = _row_counts(
        snapshot_database,
        tables=(*LEGACY_TABLES, "doc_fts"),
    )
    snapshot_config = _alembic_config(snapshot_database)
    command.stamp(snapshot_config, BASELINE_REVISION)
    command.upgrade(snapshot_config, "head")
    assert _current_revision(snapshot_database) == HEAD_REVISION
    assert _schema_signature(snapshot_database) == migrated_signature
    after_counts = _row_counts(
        snapshot_database,
        tables=(*LEGACY_TABLES, "doc_fts"),
    )
    for table_name in LEGACY_TABLES:
        if table_name not in {"permissions", "role_permissions"}:
            assert after_counts[table_name] == before_counts[table_name]
    assert after_counts["doc_fts"] == before_counts["doc_fts"]
    assert _row_counts(
        snapshot_database,
        tables=("access_rules", "group_memberships", "groups"),
    ) == {
        "access_rules": 0,
        "group_memberships": 0,
        "groups": 0,
    }


def test_baseline_downgrade_removes_application_schema(tmp_path: Path) -> None:
    database = tmp_path / "downgrade.db"
    config = _alembic_config(database)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    assert _current_revision(database) is None
    with sqlite3.connect(database) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
    assert remaining <= {"alembic_version"}


def test_alembic_refuses_an_implicit_database_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCVAULT_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DOCVAULT_DATABASE_URL"):
        command.current(Config(str(ALEMBIC_INI)))
