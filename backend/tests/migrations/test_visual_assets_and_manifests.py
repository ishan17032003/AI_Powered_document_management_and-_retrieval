from __future__ import annotations

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


def test_visual_schema_is_empty_safe_and_downgradeable(tmp_path: Path) -> None:
    database = tmp_path / "visual-assets.db"
    config = _config(database)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        assert {
            "visual_assets",
            "visual_asset_lineage",
            "visual_processing_manifests",
            "visual_retrieval_manifests",
        }.issubset(inspector.get_table_names())
        assert {"asset_key", "version_id", "lifecycle_state"}.issubset(
            column["name"] for column in inspector.get_columns("visual_assets")
        )
        assert {
            "stage",
            "attempt_count",
            "next_attempt_at",
            "error_message",
        }.issubset(column["name"] for column in inspector.get_columns("visual_processing_manifests"))
        assert {"stage", "attempt_count", "next_attempt_at", "error_message"}.issubset(
            column["name"] for column in inspector.get_columns("visual_processing_manifests")
        )
        assert {"visual_extractions"}.issubset(inspector.get_table_names())
        extraction_columns = {column["name"] for column in inspector.get_columns("visual_extractions")}
        assert {"asset_id", "version_id", "output_type", "engine_revision", "trusted", "quality_signals"}.issubset(extraction_columns)
    finally:
        engine.dispose()

    command.downgrade(config, "20260727_0010")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "visual_assets" not in tables


def test_visual_asset_constraints_reject_invalid_state(tmp_path: Path) -> None:
    database = tmp_path / "visual-constraints.db"
    command.upgrade(_config(database), "head")
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO visual_assets(
                    asset_key, document_id, version_id, asset_type, file_key,
                    content_type, checksum, lifecycle_state, created_at, updated_at
                ) VALUES ('asset-1', 1, 1, 'UNSUPPORTED', 'x', 'image/png',
                          'a', 'ACTIVE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
