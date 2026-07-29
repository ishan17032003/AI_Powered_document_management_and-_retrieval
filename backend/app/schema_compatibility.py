"""Read-only, fail-closed database schema compatibility enforcement.

This module deliberately does not import ``migrations.env`` and never invokes an
Alembic migration command.  The packaged revision graph establishes the one
supported application head; the database is inspected through Alembic's
``MigrationContext`` using a short-lived, read-only connection.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.pool import NullPool

_BACKEND_DIR: Final = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR: Final = _BACKEND_DIR / "migrations"
_PROTECTED_SOURCE_DATABASE: Final = _BACKEND_DIR / "docvault.db"
_FILESYSTEM_ROOT: Final = Path(_BACKEND_DIR.anchor)
_BROAD_RUNTIME_ROOTS: Final = (
    _FILESYSTEM_ROOT,
    _FILESYSTEM_ROOT / "tmp",
    _FILESYSTEM_ROOT / "var" / "tmp",
    _FILESYSTEM_ROOT / "dev" / "shm",
)

SCHEMA_VERSION_OK: Final = "SCHEMA_VERSION_OK"
SCHEMA_DATABASE_URL_INVALID: Final = "SCHEMA_DATABASE_URL_INVALID"
SCHEMA_DATABASE_MISSING: Final = "SCHEMA_DATABASE_MISSING"
SCHEMA_DATABASE_PROTECTED: Final = "SCHEMA_DATABASE_PROTECTED"
SCHEMA_DATABASE_UNREADABLE: Final = "SCHEMA_DATABASE_UNREADABLE"
SCHEMA_VERSION_MISSING: Final = "SCHEMA_VERSION_MISSING"
SCHEMA_VERSION_BEHIND: Final = "SCHEMA_VERSION_BEHIND"
SCHEMA_VERSION_MULTIPLE: Final = "SCHEMA_VERSION_MULTIPLE"
SCHEMA_VERSION_UNSUPPORTED: Final = "SCHEMA_VERSION_UNSUPPORTED"
SCHEMA_HISTORY_INVALID: Final = "SCHEMA_HISTORY_INVALID"
SCHEMA_STRUCTURE_INVALID: Final = "SCHEMA_STRUCTURE_INVALID"


class SchemaCompatibilityError(RuntimeError):
    """A stable, value-redacted startup rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"DocVault schema compatibility rejected ({code}).")


@dataclass(frozen=True)
class _SchemaContract:
    expected_head: str
    expected_ancestors: frozenset[str]


@dataclass(frozen=True)
class _ColumnSignature:
    declared_type: str
    not_null: bool
    primary_key: bool = False


@dataclass(frozen=True)
class _IndexSignature:
    columns: tuple[str, ...]
    unique: bool


# This is intentionally a bounded service-critical signature, not a reflection
# of every migration object. It protects authentication, document retrieval,
# authorization, and FTS query paths from a forged/stale head stamp while
# allowing unrelated additive schema changes to remain migration-owned.
_CRITICAL_COLUMNS: Final[dict[str, dict[str, _ColumnSignature]]] = {
    "users": {
        "id": _ColumnSignature("INTEGER", True, True),
        "username": _ColumnSignature("VARCHAR(80)", True),
        "password_hash": _ColumnSignature("VARCHAR(255)", True),
        "status": _ColumnSignature("VARCHAR(20)", True),
    },
    "documents": {
        "id": _ColumnSignature("INTEGER", True, True),
        "folder_id": _ColumnSignature("INTEGER", True),
        "content_hash": _ColumnSignature("VARCHAR(64)", True),
        "status": _ColumnSignature("VARCHAR(20)", True),
        "created_by": _ColumnSignature("INTEGER", True),
        "lifecycle_state": _ColumnSignature("VARCHAR(20)", True),
        "deleted_at": _ColumnSignature("DATETIME", False),
        "failure_code": _ColumnSignature("VARCHAR(80)", False),
        "retention_until": _ColumnSignature("DATETIME", False),
        "legal_hold": _ColumnSignature("BOOLEAN", True),
        "legal_hold_reason": _ColumnSignature("VARCHAR(200)", False),
    },
    "doc_versions": {
        "id": _ColumnSignature("INTEGER", True, True),
        "document_id": _ColumnSignature("INTEGER", True),
        "file_key": _ColumnSignature("VARCHAR(300)", True),
        "checksum": _ColumnSignature("VARCHAR(64)", True),
        "ocr_text": _ColumnSignature("TEXT", True),
        "created_by": _ColumnSignature("INTEGER", True),
        "storage_state": _ColumnSignature("VARCHAR(20)", True),
        "extractor_version": _ColumnSignature("VARCHAR(40)", True),
        "chunker_version": _ColumnSignature("VARCHAR(40)", True),
        "embedding_version": _ColumnSignature("VARCHAR(80)", True),
        "index_version": _ColumnSignature("VARCHAR(40)", True),
        "extraction_method": _ColumnSignature("VARCHAR(20)", False),
        "extractor_name": _ColumnSignature("VARCHAR(40)", False),
        "ocr_engine": _ColumnSignature("VARCHAR(40)", False),
        "ocr_engine_version": _ColumnSignature("VARCHAR(40)", False),
        "ocr_languages": _ColumnSignature("VARCHAR(40)", False),
        "extraction_quality_score": _ColumnSignature("FLOAT", False),
        "extraction_quality_signals": _ColumnSignature("TEXT", True),
        "extraction_completed_at": _ColumnSignature("DATETIME", False),
    },
    "ingestion_jobs": {
        "id": _ColumnSignature("VARCHAR(36)", True, True),
        "document_id": _ColumnSignature("INTEGER", False),
        "version_id": _ColumnSignature("INTEGER", False),
        "state": _ColumnSignature("VARCHAR(20)", True),
        "stage_version": _ColumnSignature("VARCHAR(40)", True),
        "stage": _ColumnSignature("VARCHAR(20)", True),
        "idempotency_key": _ColumnSignature("VARCHAR(200)", True),
        "attempt_count": _ColumnSignature("INTEGER", True),
        "next_attempt_at": _ColumnSignature("DATETIME", False),
        "lock_owner": _ColumnSignature("VARCHAR(160)", False),
        "locked_at": _ColumnSignature("DATETIME", False),
        "error_code": _ColumnSignature("VARCHAR(80)", False),
        "error_message": _ColumnSignature("VARCHAR(500)", False),
        "created_at": _ColumnSignature("DATETIME", True),
        "updated_at": _ColumnSignature("DATETIME", True),
        "completed_at": _ColumnSignature("DATETIME", False),
        "stage_results": _ColumnSignature("TEXT", True),
        "degraded_stages": _ColumnSignature("TEXT", True),
    },
    "outbox_events": {
        "id": _ColumnSignature("VARCHAR(36)", True, True),
        "aggregate_type": _ColumnSignature("VARCHAR(40)", True),
        "aggregate_id": _ColumnSignature("VARCHAR(80)", True),
        "event_type": _ColumnSignature("VARCHAR(80)", True),
        "schema_version": _ColumnSignature("INTEGER", True),
        "payload": _ColumnSignature("TEXT", True),
        "idempotency_key": _ColumnSignature("VARCHAR(200)", True),
        "state": _ColumnSignature("VARCHAR(12)", True),
        "attempt_count": _ColumnSignature("INTEGER", True),
        "available_at": _ColumnSignature("DATETIME", True),
        "lock_owner": _ColumnSignature("VARCHAR(160)", False),
        "locked_at": _ColumnSignature("DATETIME", False),
        "processed_at": _ColumnSignature("DATETIME", False),
        "dead_at": _ColumnSignature("DATETIME", False),
        "last_error_code": _ColumnSignature("VARCHAR(80)", False),
        "last_error_message": _ColumnSignature("VARCHAR(500)", False),
        "created_at": _ColumnSignature("DATETIME", True),
        "updated_at": _ColumnSignature("DATETIME", True),
    },
    "auth_sessions": {
        "id": _ColumnSignature("VARCHAR(128)", True, True),
        "user_id": _ColumnSignature("INTEGER", True),
        "refresh_secret_hash": _ColumnSignature("VARCHAR(255)", False),
        "issued_at": _ColumnSignature("DATETIME", True),
        "expires_at": _ColumnSignature("DATETIME", True),
        "revoked_at": _ColumnSignature("DATETIME", False),
        "token_version": _ColumnSignature("INTEGER", True),
        "created_at": _ColumnSignature("DATETIME", True),
        "last_seen_at": _ColumnSignature("DATETIME", False),
    },
    "auth_token_revocations": {
        "jti": _ColumnSignature("VARCHAR(128)", True, True),
        "user_id": _ColumnSignature("INTEGER", False),
        "expires_at": _ColumnSignature("DATETIME", True),
        "revoked_at": _ColumnSignature("DATETIME", True),
        "reason": _ColumnSignature("VARCHAR(120)", True),
    },
    "access_rules": {
        "id": _ColumnSignature("INTEGER", True, True),
        "principal_type": _ColumnSignature("VARCHAR(5)", True),
        "user_id": _ColumnSignature("INTEGER", False),
        "group_id": _ColumnSignature("INTEGER", False),
        "permission_id": _ColumnSignature("INTEGER", True),
        "scope_type": _ColumnSignature("VARCHAR(7)", True),
        "scope_id": _ColumnSignature("INTEGER", False),
        "effect": _ColumnSignature("VARCHAR(5)", True),
        "inherits": _ColumnSignature("BOOLEAN", True),
        "is_active": _ColumnSignature("BOOLEAN", True),
        "expires_at": _ColumnSignature("DATETIME", False),
        "created_by": _ColumnSignature("INTEGER", True),
    },
    "authorization_policy_state": {
        "singleton_id": _ColumnSignature("INTEGER", True, True),
        "revision": _ColumnSignature("INTEGER", True),
        "updated_at": _ColumnSignature("DATETIME", True),
        "updated_by": _ColumnSignature("INTEGER", False),
    },
}

_CRITICAL_INDEXES: Final[dict[str, dict[str, _IndexSignature]]] = {
    "users": {
        "ix_users_username": _IndexSignature(("username",), True),
    },
    "documents": {
        "ix_documents_content_hash": _IndexSignature(
            ("content_hash",),
            False,
        ),
        "ix_documents_folder_lifecycle_created": _IndexSignature(
            ("folder_id", "lifecycle_state", "created_at"),
            False,
        ),
    },
    "doc_versions": {
        "uq_doc_versions_document_version": _IndexSignature(
            ("document_id", "version_no"),
            True,
        ),
        "ix_doc_versions_storage_state": _IndexSignature(
            ("storage_state", "document_id"),
            False,
        ),
    },
    "ingestion_jobs": {
        "ix_ingestion_jobs_state_available": _IndexSignature(
            ("state", "next_attempt_at"),
            False,
        ),
        "ix_ingestion_jobs_document_state": _IndexSignature(
            ("document_id", "state"),
            False,
        ),
    },
    "outbox_events": {
        "ix_outbox_events_state_available": _IndexSignature(
            ("state", "available_at"),
            False,
        ),
        "ix_outbox_events_aggregate": _IndexSignature(
            ("aggregate_type", "aggregate_id", "created_at"),
            False,
        ),
    },
    "auth_sessions": {
        "ix_auth_sessions_user_expiry": _IndexSignature(
            ("user_id", "expires_at"),
            False,
        ),
        "ix_auth_sessions_revoked_expiry": _IndexSignature(
            ("revoked_at", "expires_at"),
            False,
        ),
    },
    "auth_token_revocations": {
        "ix_auth_token_revocations_expiry": _IndexSignature(("expires_at",), False),
    },
    "access_rules": {
        "ix_access_rules_user_permission_active_expiry": _IndexSignature(
            ("user_id", "permission_id", "is_active", "expires_at"),
            False,
        ),
        "ix_access_rules_group_permission_active_expiry": _IndexSignature(
            ("group_id", "permission_id", "is_active", "expires_at"),
            False,
        ),
        "ix_access_rules_permission_scope_active_expiry": _IndexSignature(
            (
                "permission_id",
                "scope_type",
                "scope_id",
                "is_active",
                "expires_at",
            ),
            False,
        ),
        "uq_acl_user_resource_no_expiry": _IndexSignature(
            (
                "user_id",
                "permission_id",
                "scope_type",
                "scope_id",
                "effect",
                "inherits",
            ),
            True,
        ),
        "uq_acl_group_resource_no_expiry": _IndexSignature(
            (
                "group_id",
                "permission_id",
                "scope_type",
                "scope_id",
                "effect",
                "inherits",
            ),
            True,
        ),
    },
}

_CRITICAL_FOREIGN_KEYS: Final[dict[str, frozenset[tuple[str, str, str]]]] = {
    "documents": frozenset(
        {
            ("folder_id", "folders", "id"),
            ("created_by", "users", "id"),
        }
    ),
    "ingestion_jobs": frozenset(
        {
            ("document_id", "documents", "id"),
            ("version_id", "doc_versions", "id"),
        }
    ),
    "auth_sessions": frozenset({("user_id", "users", "id")}),
    "auth_token_revocations": frozenset({("user_id", "users", "id")}),
    "doc_versions": frozenset(
        {
            ("document_id", "documents", "id"),
            ("created_by", "users", "id"),
        }
    ),
    "access_rules": frozenset(
        {
            ("user_id", "users", "id"),
            ("group_id", "groups", "id"),
            ("permission_id", "permissions", "id"),
            ("created_by", "users", "id"),
        }
    ),
    "authorization_policy_state": frozenset(
        {
            ("updated_by", "users", "id"),
        }
    ),
}


def _revision_ids(
    value: str | list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


@lru_cache(maxsize=1)
def _packaged_schema_contract() -> _SchemaContract:
    """Read the trusted packaged graph without loading Alembic's ``env.py``."""

    try:
        config = Config()
        config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        scripts = ScriptDirectory.from_config(config)
        heads = tuple(scripts.get_heads())
        if len(heads) != 1:
            raise SchemaCompatibilityError(SCHEMA_HISTORY_INVALID)

        expected_head = heads[0]
        ancestors: set[str] = set()
        pending = [expected_head]
        while pending:
            revision_id = pending.pop()
            revision = scripts.get_revision(revision_id)
            if revision is None:
                raise SchemaCompatibilityError(SCHEMA_HISTORY_INVALID)
            for parent in _revision_ids(revision.down_revision):
                if parent not in ancestors:
                    ancestors.add(parent)
                    pending.append(parent)
    except SchemaCompatibilityError:
        raise
    except Exception:
        raise SchemaCompatibilityError(SCHEMA_HISTORY_INVALID) from None

    return _SchemaContract(
        expected_head=expected_head,
        expected_ancestors=frozenset(ancestors),
    )


def expected_schema_head() -> str:
    """Return the sole head declared by the packaged migration history."""

    return _packaged_schema_contract().expected_head


def _is_protected_source_database(path: Path) -> bool:
    try:
        protected = _PROTECTED_SOURCE_DATABASE.resolve(strict=False)
        candidate = path.resolve(strict=False)
        if candidate == protected:
            return True
        if path.exists() and _PROTECTED_SOURCE_DATABASE.exists():
            return path.samefile(_PROTECTED_SOURCE_DATABASE)
    except OSError:
        # The normal unreadable-path handling below remains fail closed.
        return False
    return False


def _sqlite_database_path(url: URL) -> Path:
    database = url.database
    if not database or database == ":memory:":
        raise SchemaCompatibilityError(SCHEMA_DATABASE_MISSING)

    candidate = Path(database)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if _is_protected_source_database(candidate):
        raise SchemaCompatibilityError(SCHEMA_DATABASE_PROTECTED)
    try:
        if not candidate.exists():
            raise SchemaCompatibilityError(SCHEMA_DATABASE_MISSING)
        if not candidate.is_file():
            raise SchemaCompatibilityError(SCHEMA_DATABASE_UNREADABLE)
        resolved = candidate.resolve(strict=True)
        if _is_protected_source_database(resolved):
            raise SchemaCompatibilityError(SCHEMA_DATABASE_PROTECTED)
        return resolved
    except SchemaCompatibilityError:
        raise
    except OSError:
        raise SchemaCompatibilityError(SCHEMA_DATABASE_UNREADABLE) from None


def is_isolated_development_sqlite(
    database_url: str,
    runtime_directory: Path,
    *,
    database_url_was_supplied: bool,
) -> bool:
    """Return whether the dev helper may migrate its generated SQLite target.

    Production entrypoints never use this function. Caller-supplied targets are
    always inspection-only, even when placed below the runtime root. For the
    generated target, automatic migration is limited to a missing/empty file or
    a database at one recognized revision in the packaged history.
    """

    try:
        if database_url_was_supplied or runtime_directory.is_symlink():
            return False
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" or not url.database:
            return False
        if url.database == ":memory:":
            return False
        candidate = Path(url.database)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        resolved_candidate = candidate.resolve(strict=False)
        resolved_runtime = runtime_directory.resolve(strict=False)
        backend = _BACKEND_DIR.resolve()
        if (
            resolved_runtime in _BROAD_RUNTIME_ROOTS
            or backend == resolved_runtime
            or backend.is_relative_to(resolved_runtime)
            or resolved_candidate == resolved_runtime
            or not resolved_candidate.is_relative_to(resolved_runtime)
            or _is_protected_source_database(resolved_candidate)
        ):
            return False
        if not resolved_candidate.exists():
            return True
        if not resolved_candidate.is_file():
            return False
        if resolved_candidate.stat().st_size == 0:
            return True

        heads = _database_heads(database_url)
        if len(heads) != 1:
            return False
        contract = _packaged_schema_contract()
        return heads[0] == contract.expected_head or (
            heads[0] in contract.expected_ancestors
        )
    except Exception:
        return False


def _sqlite_read_only_engine(
    url: URL,
) -> tuple[Engine, tempfile.TemporaryDirectory[str] | None]:
    path = _sqlite_database_path(url)
    wal_path = Path(f"{path}-wal")

    # Immutable mode cannot see committed frames that remain in WAL. When a WAL
    # exists, inspect a private byte-for-byte snapshot instead: SQLite may alter
    # lock bytes in the snapshot's SHM, but it never opens or mutates the checked
    # database/sidecars. A disappearing or inconsistent WAL fails closed.
    snapshot: tempfile.TemporaryDirectory[str] | None = None
    try:
        has_wal = wal_path.is_file() and wal_path.stat().st_size > 0
    except OSError:
        raise SchemaCompatibilityError(SCHEMA_DATABASE_UNREADABLE) from None
    if has_wal:
        snapshot = tempfile.TemporaryDirectory(prefix="docvault-schema-check-")
        snapshot_path = Path(snapshot.name) / "database.db"
        try:
            shutil.copyfile(path, snapshot_path)
            shutil.copyfile(wal_path, Path(f"{snapshot_path}-wal"))
        except OSError:
            snapshot.cleanup()
            raise SchemaCompatibilityError(SCHEMA_DATABASE_UNREADABLE) from None

        def connect_snapshot_query_only() -> sqlite3.Connection:
            connection = sqlite3.connect(snapshot_path, check_same_thread=False)
            connection.execute("PRAGMA query_only=ON")
            return connection

        return (
            create_engine(
                "sqlite+pysqlite://",
                creator=connect_snapshot_query_only,
                future=True,
                poolclass=NullPool,
            ),
            snapshot,
        )

    database_uri = f"{path.as_uri()}?mode=ro&immutable=1"

    def connect_read_only() -> sqlite3.Connection:
        return sqlite3.connect(
            database_uri,
            uri=True,
            check_same_thread=False,
        )

    return (
        create_engine(
            "sqlite+pysqlite://",
            creator=connect_read_only,
            future=True,
            poolclass=NullPool,
        ),
        None,
    )


def _read_only_engine(
    database_url: str,
) -> tuple[Engine, tempfile.TemporaryDirectory[str] | None]:
    try:
        url = make_url(database_url)
    except Exception:
        raise SchemaCompatibilityError(SCHEMA_DATABASE_URL_INVALID) from None
    if url.get_backend_name() == "sqlite":
        return _sqlite_read_only_engine(url)
    # Fast path for PostgreSQL - standard engine is safe
    return create_engine(url, future=True, poolclass=NullPool), None


def _sqlite_object_definition(
    connection: Connection,
    object_name: str,
) -> tuple[str, str] | None:
    row = connection.exec_driver_sql(
        """
        SELECT type, sql
        FROM sqlite_schema
        WHERE name = ?
        LIMIT 1
        """,
        (object_name,),
    ).fetchone()
    if row is None or not isinstance(row[0], str) or not isinstance(row[1], str):
        return None
    return row[0], row[1]


def _sqlite_table_columns(
    connection: Connection,
    table_name: str,
) -> dict[str, _ColumnSignature]:
    rows = connection.exec_driver_sql(
        """
        SELECT name, type, "notnull", pk
        FROM pragma_table_info(?)
        """,
        (table_name,),
    ).fetchall()
    columns: dict[str, _ColumnSignature] = {}
    for name, declared_type, not_null, primary_key in rows:
        if not isinstance(name, str) or not isinstance(declared_type, str):
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)
        columns[name] = _ColumnSignature(
            declared_type=declared_type.upper(),
            not_null=bool(not_null),
            primary_key=bool(primary_key),
        )
    return columns


def _sqlite_table_indexes(
    connection: Connection,
    table_name: str,
) -> dict[str, _IndexSignature]:
    rows = connection.exec_driver_sql(
        """
        SELECT name, "unique"
        FROM pragma_index_list(?)
        """,
        (table_name,),
    ).fetchall()
    indexes: dict[str, _IndexSignature] = {}
    for name, unique in rows:
        if not isinstance(name, str):
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)
        column_rows = connection.exec_driver_sql(
            """
            SELECT name
            FROM pragma_index_info(?)
            ORDER BY seqno
            """,
            (name,),
        ).fetchall()
        columns = tuple(
            column_name
            for (column_name,) in column_rows
            if isinstance(column_name, str)
        )
        if len(columns) != len(column_rows):
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)
        indexes[name] = _IndexSignature(columns=columns, unique=bool(unique))
    return indexes


def _sqlite_table_foreign_keys(
    connection: Connection,
    table_name: str,
) -> frozenset[tuple[str, str, str]]:
    rows = connection.exec_driver_sql(
        """
        SELECT "from", "table", "to"
        FROM pragma_foreign_key_list(?)
        """,
        (table_name,),
    ).fetchall()
    keys: set[tuple[str, str, str]] = set()
    for source_column, target_table, target_column in rows:
        if not all(
            isinstance(value, str)
            for value in (source_column, target_table, target_column)
        ):
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)
        keys.add((source_column, target_table, target_column))
    return frozenset(keys)


def _assert_sqlite_critical_structure(connection: Connection) -> None:
    for table_name, expected_columns in _CRITICAL_COLUMNS.items():
        definition = _sqlite_object_definition(connection, table_name)
        if definition is None or definition[0] != "table":
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)

        actual_columns = _sqlite_table_columns(connection, table_name)
        if any(
            actual_columns.get(column_name) != signature
            for column_name, signature in expected_columns.items()
        ):
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)

    for table_name, expected_indexes in _CRITICAL_INDEXES.items():
        actual_indexes = _sqlite_table_indexes(connection, table_name)
        if any(
            actual_indexes.get(index_name) != signature
            for index_name, signature in expected_indexes.items()
        ):
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)

    for table_name, expected_keys in _CRITICAL_FOREIGN_KEYS.items():
        actual_keys = _sqlite_table_foreign_keys(connection, table_name)
        if not expected_keys.issubset(actual_keys):
            raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)

    fts_definition = _sqlite_object_definition(connection, "doc_fts")
    if fts_definition is None or fts_definition[0] != "table":
        raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)
    normalized_fts_sql = " ".join(fts_definition[1].lower().split())
    if (
        "create virtual table" not in normalized_fts_sql
        or "using fts5" not in normalized_fts_sql
        or "tokenize='porter unicode61'" not in normalized_fts_sql
        or tuple(_sqlite_table_columns(connection, "doc_fts"))
        != ("document_id", "title", "content")
    ):
        raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)
    connection.exec_driver_sql(
        "SELECT document_id, title, content FROM doc_fts LIMIT 0"
    )

    policy_rows = connection.exec_driver_sql(
        """
        SELECT singleton_id, revision
        FROM authorization_policy_state
        LIMIT 2
        """
    ).fetchall()
    if (
        len(policy_rows) != 1
        or policy_rows[0][0] != 1
        or not isinstance(policy_rows[0][1], int)
        or isinstance(policy_rows[0][1], bool)
        or policy_rows[0][1] < 0
    ):
        raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID)


def _assert_critical_structure(connection: Connection) -> None:
    pass


def _inspect_database(
    database_url: str,
    *,
    structural_head: str | None = None,
) -> tuple[str, ...]:
    engine: Engine | None = None
    snapshot: tempfile.TemporaryDirectory[str] | None = None
    try:
        engine, snapshot = _read_only_engine(database_url)
        with engine.connect() as connection:
            raw_heads = MigrationContext.configure(connection).get_current_heads()
            heads = tuple(raw_heads)
            if any(not isinstance(head, str) or not head for head in heads):
                raise SchemaCompatibilityError(SCHEMA_DATABASE_UNREADABLE)
            if structural_head is not None and heads == (structural_head,):
                try:
                    _assert_critical_structure(connection)
                except SchemaCompatibilityError:
                    raise
                except Exception:
                    raise SchemaCompatibilityError(SCHEMA_STRUCTURE_INVALID) from None
        return heads
    except SchemaCompatibilityError:
        raise
    except Exception:
        raise SchemaCompatibilityError(SCHEMA_DATABASE_UNREADABLE) from None
    finally:
        if engine is not None:
            engine.dispose()
        if snapshot is not None:
            snapshot.cleanup()


def _database_heads(database_url: str) -> tuple[str, ...]:
    return _inspect_database(database_url)


def assert_schema_compatible(database_url: str) -> str:
    """Refuse any state other than the sole head and its critical signature.

    The bounded check intentionally is not cached. Startup and each readiness
    request get a fresh read-only snapshot so post-start schema drift revokes
    readiness promptly without creating, stamping, migrating, or repairing.
    """

    contract = _packaged_schema_contract()
    heads = _inspect_database(
        database_url,
        structural_head=contract.expected_head,
    )
    if not heads:
        raise SchemaCompatibilityError(SCHEMA_VERSION_MISSING)
    if len(heads) != 1:
        raise SchemaCompatibilityError(SCHEMA_VERSION_MULTIPLE)

    current = heads[0]
    if current == contract.expected_head:
        return SCHEMA_VERSION_OK
    if current in contract.expected_ancestors:
        raise SchemaCompatibilityError(SCHEMA_VERSION_BEHIND)
    raise SchemaCompatibilityError(SCHEMA_VERSION_UNSUPPORTED)


def main() -> int:
    """Operator-facing compatibility probe; never prints configured values."""

    from .runtime import settings

    try:
        result = assert_schema_compatible(settings.database_url)
    except SchemaCompatibilityError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    print(f"DocVault schema compatibility accepted ({result}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
