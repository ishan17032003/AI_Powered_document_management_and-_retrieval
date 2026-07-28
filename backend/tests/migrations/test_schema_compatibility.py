"""Fail-closed, read-only coverage for the application schema gate."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app import schema_compatibility

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
BASELINE_REVISION = "20260727_0001"
HEAD_REVISION = "20260727_0004"


def _config(database: Path) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _database_url(database: Path) -> str:
    return f"sqlite:///{database}"


def _file_state(root: Path) -> dict[str, tuple[int, int, str]]:
    state: dict[str, tuple[int, int, str]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        state[str(path.relative_to(root))] = (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(payload).hexdigest(),
        )
    return state


def _assert_rejected_without_mutation(
    database: Path,
    expected_code: str,
) -> None:
    before = _file_state(database.parent)

    with pytest.raises(schema_compatibility.SchemaCompatibilityError) as raised:
        schema_compatibility.assert_schema_compatible(_database_url(database))

    assert raised.value.code == expected_code
    assert str(raised.value) == (
        f"DocVault schema compatibility rejected ({expected_code})."
    )
    assert str(database) not in str(raised.value)
    assert _file_state(database.parent) == before


def test_packaged_history_has_one_reviewed_linear_head() -> None:
    assert schema_compatibility.expected_schema_head() == HEAD_REVISION


def test_exact_head_passes_without_file_mutation(tmp_path: Path) -> None:
    database = tmp_path / "head.db"
    command.upgrade(_config(database), "head")
    before = _file_state(tmp_path)

    assert (
        schema_compatibility.assert_schema_compatible(_database_url(database))
        == schema_compatibility.SCHEMA_VERSION_OK
    )

    assert _file_state(tmp_path) == before


def test_committed_uncheckpointed_wal_head_is_visible_without_source_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wal-head.db"
    command.upgrade(_config(database), "head")
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE alembic_version SET version_num = ?",
            ("20260727_0002",),
        )
        writer.commit()
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.execute(
            "UPDATE alembic_version SET version_num = ?",
            (HEAD_REVISION,),
        )
        writer.commit()
        assert Path(f"{database}-wal").stat().st_size > 0
        before = _file_state(tmp_path)

        assert (
            schema_compatibility.assert_schema_compatible(_database_url(database))
            == schema_compatibility.SCHEMA_VERSION_OK
        )

        assert _file_state(tmp_path) == before
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("critical_table", "drop_statement"),
    (
        ("users", "DROP TABLE users"),
        ("documents", "DROP TABLE documents"),
        ("doc_versions", "DROP TABLE doc_versions"),
        ("access_rules", "DROP TABLE access_rules"),
        (
            "authorization_policy_state",
            "DROP TABLE authorization_policy_state",
        ),
        ("doc_fts", "DROP TABLE doc_fts"),
    ),
)
def test_head_with_missing_critical_table_is_rejected_without_mutation(
    tmp_path: Path,
    critical_table: str,
    drop_statement: str,
) -> None:
    database = tmp_path / f"missing-{critical_table}.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute(drop_statement)

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_STRUCTURE_INVALID,
    )


def test_head_stamp_without_schema_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stamp-only-operator-canary.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE alembic_version(
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            )
            """
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (HEAD_REVISION,),
        )

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_STRUCTURE_INVALID,
    )


def test_head_with_renamed_critical_column_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "renamed-column-operator-canary.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE users RENAME COLUMN status TO account_status")

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_STRUCTURE_INVALID,
    )


def test_head_with_missing_critical_index_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-index-operator-canary.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ix_access_rules_permission_scope_active_expiry")

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_STRUCTURE_INVALID,
    )


def test_plain_table_cannot_impersonate_the_fts5_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fake-fts-operator-canary.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE doc_fts")
        connection.execute(
            """
            CREATE TABLE doc_fts(
                document_id,
                title,
                content
            )
            """
        )

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_STRUCTURE_INVALID,
    )


def test_missing_policy_singleton_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-policy-state-operator-canary.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM authorization_policy_state")

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_STRUCTURE_INVALID,
    )


def test_head_with_missing_critical_foreign_key_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-foreign-key-operator-canary.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            ALTER TABLE authorization_policy_state
            RENAME TO authorization_policy_state_old;

            CREATE TABLE authorization_policy_state(
                singleton_id INTEGER NOT NULL PRIMARY KEY,
                revision INTEGER NOT NULL,
                updated_at DATETIME NOT NULL,
                updated_by INTEGER
            );

            INSERT INTO authorization_policy_state(
                singleton_id,
                revision,
                updated_at,
                updated_by
            )
            SELECT singleton_id, revision, updated_at, updated_by
            FROM authorization_policy_state_old;

            DROP TABLE authorization_policy_state_old;
            """
        )

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_STRUCTURE_INVALID,
    )


def test_uncheckpointed_wal_structure_drift_is_detected_without_source_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wal-structure-operator-canary.db"
    command.upgrade(_config(database), "head")
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.execute("DROP INDEX ix_users_username")
        writer.commit()
        assert Path(f"{database}-wal").stat().st_size > 0
        before = _file_state(tmp_path)

        with pytest.raises(schema_compatibility.SchemaCompatibilityError) as raised:
            schema_compatibility.assert_schema_compatible(_database_url(database))

        assert raised.value.code == schema_compatibility.SCHEMA_STRUCTURE_INVALID
        assert "operator-canary" not in str(raised.value)
        assert _file_state(tmp_path) == before
    finally:
        writer.close()


def test_baseline_revision_is_rejected_as_behind_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "behind.db"
    command.upgrade(_config(database), BASELINE_REVISION)

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_VERSION_BEHIND,
    )


@pytest.mark.parametrize(
    "fabricated_revision",
    ["20990101_future", "not-a-packaged-revision"],
)
def test_unknown_or_future_revision_is_rejected_without_mutation(
    tmp_path: Path,
    fabricated_revision: str,
) -> None:
    database = tmp_path / "unsupported.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = ?",
            (fabricated_revision,),
        )

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_VERSION_UNSUPPORTED,
    )


def test_multiple_database_heads_are_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "multiple.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (BASELINE_REVISION,),
        )

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_VERSION_MULTIPLE,
    )


def test_missing_version_table_is_rejected_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "unversioned.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('must-survive')")

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_VERSION_MISSING,
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchall() == [
            ("must-survive",)
        ]


def test_missing_database_is_rejected_without_creating_any_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing.db"

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_DATABASE_MISSING,
    )

    assert not database.exists()
    assert not Path(f"{database}-journal").exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_malformed_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "malformed.db"
    database.write_bytes(b"not a sqlite database\0operator-canary")

    _assert_rejected_without_mutation(
        database,
        schema_compatibility.SCHEMA_DATABASE_UNREADABLE,
    )


def test_checked_in_database_is_rejected_before_opening_it() -> None:
    source_database = BACKEND_DIR / "docvault.db"
    assert source_database.is_file()

    with pytest.raises(schema_compatibility.SchemaCompatibilityError) as raised:
        schema_compatibility.assert_schema_compatible(
            _database_url(source_database),
        )

    assert raised.value.code == schema_compatibility.SCHEMA_DATABASE_PROTECTED


def test_cli_output_is_stable_and_redacted(
    tmp_path: Path,
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert settings_env["DOCVAULT_ENVIRONMENT"] == "test"
    from app import runtime

    database = tmp_path / "operator-canary.db"
    monkeypatch.setattr(runtime.settings, "database_url", _database_url(database))

    assert schema_compatibility.main() == 78
    output = capsys.readouterr()
    assert output.out == ""
    assert schema_compatibility.SCHEMA_DATABASE_MISSING in output.err
    assert "operator-canary" not in output.err


def test_cli_redacts_structure_drift_details(
    tmp_path: Path,
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert settings_env["DOCVAULT_ENVIRONMENT"] == "test"
    from app import runtime

    database = tmp_path / "structure-sql-env-operator-canary.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ix_users_username")
    monkeypatch.setattr(runtime.settings, "database_url", _database_url(database))
    capsys.readouterr()

    assert schema_compatibility.main() == 78
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.endswith(
        "DocVault schema compatibility rejected (SCHEMA_STRUCTURE_INVALID).\n"
    )
    assert "operator-canary" not in output.err
    assert "ix_users_username" not in output.err
    assert settings_env["DOCVAULT_SECRET_KEY"] not in output.err


def test_only_generated_isolated_runtime_sqlite_is_dev_bootstrap_eligible(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    isolated = runtime / "db" / "docvault.db"
    external = tmp_path / "external" / "docvault.db"

    assert schema_compatibility.is_isolated_development_sqlite(
        _database_url(isolated),
        runtime,
        database_url_was_supplied=False,
    )
    isolated.parent.mkdir(parents=True)
    isolated.touch()
    assert schema_compatibility.is_isolated_development_sqlite(
        _database_url(isolated),
        runtime,
        database_url_was_supplied=False,
    )
    assert not schema_compatibility.is_isolated_development_sqlite(
        _database_url(isolated),
        runtime,
        database_url_was_supplied=True,
    )
    assert not schema_compatibility.is_isolated_development_sqlite(
        _database_url(external),
        runtime,
        database_url_was_supplied=False,
    )
    assert not schema_compatibility.is_isolated_development_sqlite(
        "postgresql+psycopg://database/docvault",
        runtime,
        database_url_was_supplied=False,
    )
    assert not schema_compatibility.is_isolated_development_sqlite(
        _database_url(BACKEND_DIR / "docvault.db"),
        BACKEND_DIR,
        database_url_was_supplied=False,
    )
    assert not schema_compatibility.is_isolated_development_sqlite(
        "sqlite:////runtime.db",
        Path("/"),
        database_url_was_supplied=False,
    )


def test_nonempty_unversioned_dev_database_is_inspection_only(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_data(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_data VALUES ('must-survive')")
    before = _file_state(runtime)

    assert not schema_compatibility.is_isolated_development_sqlite(
        _database_url(database),
        runtime,
        database_url_was_supplied=False,
    )

    assert _file_state(runtime) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM legacy_data").fetchall() == [
            ("must-survive",)
        ]


def test_recognized_dev_revision_is_eligible_but_unknown_is_not(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "recognized.db"
    command.upgrade(_config(database), BASELINE_REVISION)

    assert schema_compatibility.is_isolated_development_sqlite(
        _database_url(database),
        runtime,
        database_url_was_supplied=False,
    )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = 'unknown-development-revision'"
        )
    before = _file_state(runtime)
    assert not schema_compatibility.is_isolated_development_sqlite(
        _database_url(database),
        runtime,
        database_url_was_supplied=False,
    )
    assert _file_state(runtime) == before
