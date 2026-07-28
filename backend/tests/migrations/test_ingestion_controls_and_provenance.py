import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _config(database: Path) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def test_ingestion_control_and_provenance_schema(tmp_path: Path) -> None:
    database = tmp_path / "ingestion-controls.db"
    command.upgrade(_config(database), "20260727_0008")

    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        version_columns = {
            column["name"]: column
            for column in inspector.get_columns("doc_versions")
        }
        job_columns = {
            column["name"]: column
            for column in inspector.get_columns("ingestion_jobs")
        }
        assert {
            "extraction_method",
            "extractor_name",
            "ocr_engine",
            "ocr_engine_version",
            "ocr_languages",
            "extraction_quality_score",
            "extraction_quality_signals",
            "extraction_completed_at",
        }.issubset(version_columns)
        assert {"stage_results", "degraded_stages"}.issubset(job_columns)
        assert job_columns["stage"]["nullable"] is False
        assert job_columns["stage_results"]["nullable"] is False
        assert version_columns["embedding_version"]["nullable"] is False
        assert version_columns["index_version"]["nullable"] is False
    finally:
        engine.dispose()

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO ingestion_jobs(
                id, state, stage_version, stage, idempotency_key,
                attempt_count, created_at, updated_at
            ) VALUES (?, 'CANCELLED', 'pipeline-v2', 'EXTRACT', ?, 0,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            ("cancelled-job", "cancelled-job"),
        )
        connection.commit()
        assert connection.execute(
            "SELECT state, stage_results, degraded_stages "
            "FROM ingestion_jobs WHERE id = 'cancelled-job'"
        ).fetchone() == ("CANCELLED", "{}", "[]")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ingestion_jobs(
                    id, state, stage_version, stage, idempotency_key,
                    attempt_count, created_at, updated_at
                ) VALUES (?, 'UNKNOWN', 'pipeline-v2', 'EXTRACT', ?, 0,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ("invalid-job", "invalid-job"),
            )
    finally:
        connection.close()


def test_ingestion_control_migration_downgrades_to_previous_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ingestion-controls-downgrade.db"
    config = _config(database)
    command.upgrade(config, "20260727_0008")
    command.downgrade(config, "20260727_0007")

    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        assert "stage_results" not in {
            column["name"]
            for column in inspector.get_columns("ingestion_jobs")
        }
        assert "extraction_method" not in {
            column["name"] for column in inspector.get_columns("doc_versions")
        }
    finally:
        engine.dispose()
