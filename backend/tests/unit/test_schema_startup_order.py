"""Ordering coverage for every application/operator schema mutation path."""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_web_startup_checks_schema_before_runtime_directories(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = importlib.import_module("app.main")
    calls: list[str] = []
    monkeypatch.setattr(
        main,
        "assert_schema_compatible",
        lambda _url: calls.append("schema"),
    )
    monkeypatch.setattr(
        main,
        "prepare_runtime_directories",
        lambda _settings: calls.append("directories"),
    )

    main._startup()

    assert calls == ["schema", "directories"]


def test_web_startup_never_reaches_runtime_setup_after_schema_rejection(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = importlib.import_module("app.main")
    schema_compatibility = importlib.import_module("app.schema_compatibility")
    calls: list[str] = []

    def reject(_url: str) -> None:
        calls.append("schema")
        raise schema_compatibility.SchemaCompatibilityError(
            schema_compatibility.SCHEMA_VERSION_MISSING
        )

    monkeypatch.setattr(main, "assert_schema_compatible", reject)
    monkeypatch.setattr(
        main,
        "prepare_runtime_directories",
        lambda _settings: calls.append("directories"),
    )

    with pytest.raises(schema_compatibility.SchemaCompatibilityError):
        main._startup()

    assert calls == ["schema"]


def test_head_stamped_schema_drift_is_never_repaired_by_web_or_operator_clis(
    settings_env: dict[str, str],
    test_paths,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings_env["DOCVAULT_DATABASE_URL"])
    command.upgrade(config, "head")
    with sqlite3.connect(test_paths.database) as connection:
        connection.execute("DROP TABLE users")
    capsys.readouterr()

    main = importlib.import_module("app.main")
    schema_compatibility = importlib.import_module("app.schema_compatibility")
    with pytest.raises(
        schema_compatibility.SchemaCompatibilityError
    ) as startup_rejection:
        main._startup()
    assert startup_rejection.value.code == schema_compatibility.SCHEMA_STRUCTURE_INVALID
    assert str(test_paths.database) not in str(startup_rejection.value)
    with sqlite3.connect(test_paths.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone() == (0,)

    seed = importlib.import_module("app.seed")
    assert seed.main() == 78
    seed_output = capsys.readouterr()
    assert seed_output.out == ""
    assert seed_output.err.endswith(
        "DocVault schema compatibility rejected (SCHEMA_STRUCTURE_INVALID).\n"
    )
    assert str(test_paths.database) not in seed_output.err

    password_file = tmp_path / "operator-secret"
    password_file.write_text(
        "Qz9!schema-drift-must-not-be-repaired-2026",
        encoding="utf-8",
    )
    password_file.chmod(0o600)
    provision_admin = importlib.import_module("app.provision_admin")
    assert (
        provision_admin.main(
            [
                "--username",
                "first.operator",
                "--name",
                "First Operator",
                "--email",
                "first.operator@example.com",
                "--password-file",
                str(password_file),
            ]
        )
        == 78
    )
    provision_output = capsys.readouterr()
    assert provision_output.out == ""
    assert provision_output.err.endswith(
        "DocVault schema compatibility rejected (SCHEMA_STRUCTURE_INVALID).\n"
    )
    assert str(test_paths.database) not in provision_output.err
    assert str(password_file) not in provision_output.err

    with sqlite3.connect(test_paths.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall() == [("20260727_0004",)]


def test_application_runtime_contains_no_schema_creation_or_repair_call() -> None:
    runtime_sources = "\n".join(
        (BACKEND_DIR / relative).read_text(encoding="utf-8")
        for relative in (
            "app/database.py",
            "app/main.py",
            "app/seed.py",
            "app/provision_admin.py",
        )
    )

    assert "create_all" not in runtime_sources
    assert "CREATE VIRTUAL TABLE" not in runtime_sources
    assert "init_db" not in runtime_sources


def test_production_entrypoint_checks_without_running_migrations() -> None:
    entrypoint = (BACKEND_DIR / "entrypoint.sh").read_text(encoding="utf-8")

    assert "python -m app.schema_compatibility" in entrypoint
    assert entrypoint.index("python -m app.schema_compatibility") < entrypoint.index(
        "python -m app.seed"
    )
    assert "alembic upgrade" not in entrypoint
    assert "alembic stamp" not in entrypoint


def test_development_start_migration_is_guarded_by_isolated_runtime_check() -> None:
    start = (BACKEND_DIR / "start.sh").read_text(encoding="utf-8")

    assert "is_isolated_development_sqlite" in start
    assert '"${VENV_DIR}/bin/alembic" upgrade head' in start
    assert "SCHEMA_MIGRATION_REQUIRED" in start
    assert "DOCVAULT_RUNTIME_DIR" in start
    assert "DOCVAULT_DATABASE_URL_WAS_SUPPLIED" in start
    assert "validate and stamp a legacy baseline" in start


def test_enabled_seed_refuses_missing_schema_without_creating_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seed-canary.db"
    environment = {
        **os.environ,
        "DOCVAULT_ENVIRONMENT": "test",
        "DOCVAULT_ENABLE_DEMO_SEED": "true",
        "DOCVAULT_SECRET_KEY": "test-only-seed-schema-check-signing-material",
        "DOCVAULT_DATABASE_URL": f"sqlite:///{database}",
        "DOCVAULT_STORAGE_DIR": str(tmp_path / "storage"),
        "DOCVAULT_OKF_BUNDLE_DIR": str(tmp_path / "okf"),
        "DOCVAULT_LLM_PROVIDER": "none",
        "DOCVAULT_USE_DOCLING": "false",
        "DOCVAULT_USE_QDRANT": "false",
        "DOCVAULT_EMBEDDING_MODEL": "",
        "DOCVAULT_RERANKER_MODEL": "",
    }

    result = subprocess.run(
        [sys.executable, "-m", "app.seed"],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 78
    assert "SCHEMA_DATABASE_MISSING" in result.stderr
    assert "seed-canary" not in result.stdout + result.stderr
    assert not database.exists()


def test_provisioning_refuses_missing_schema_before_reading_password(
    settings_env: dict[str, str],
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provision_admin = importlib.import_module("app.provision_admin")

    def reject_password_read(_path: Path) -> str:
        raise AssertionError("password source was read before the schema gate")

    monkeypatch.setattr(provision_admin, "read_password_file", reject_password_read)
    result = provision_admin.main(
        [
            "--username",
            "first.operator",
            "--name",
            "First Operator",
            "--email",
            "first.operator@example.com",
            "--password-file",
            str(test_paths.root / "operator-canary"),
        ]
    )
    output = capsys.readouterr()

    assert settings_env["DOCVAULT_DATABASE_URL"].endswith("docvault-test.db")
    assert result == 78
    assert output.out == ""
    assert output.err.endswith(
        "DocVault schema compatibility rejected (SCHEMA_DATABASE_MISSING).\n"
    )
    assert "operator-canary" not in output.err
    assert not test_paths.database.exists()
