"""Regression coverage for read-only baseline database reporting."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.baseline_snapshot import (
    BaselineError,
    database_report,
    sqlite_table_count,
)


def _create_report_database(path: Path) -> str:
    hostile_table = 'evidence"; DROP TABLE survivor; --'
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE survivor (id INTEGER PRIMARY KEY);
            INSERT INTO survivor VALUES (1);
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                ocr_status TEXT NOT NULL
            );
            INSERT INTO documents VALUES (1, 'active', 'complete');
            INSERT INTO documents VALUES (2, 'active', 'pending');
            CREATE TABLE "evidence""; DROP TABLE survivor; --" (
                id INTEGER PRIMARY KEY
            );
            INSERT INTO "evidence""; DROP TABLE survivor; --" VALUES (1);
            INSERT INTO "evidence""; DROP TABLE survivor; --" VALUES (2);
            """
        )
    return hostile_table


def test_database_report_quotes_hostile_table_names_without_changing_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hostile-identifier.db"
    hostile_table = _create_report_database(database)

    report, schema = database_report(database)

    assert report["table_counts"][hostile_table] == 2
    assert report["table_counts"]["survivor"] == 1
    assert report["table_counts"]["documents"] == 2
    assert report["document_status_counts"] == {"active": 2}
    assert report["ocr_status_counts"] == {"complete": 1, "pending": 1}
    assert hostile_table in schema

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM survivor").fetchone() == (1,)
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert table_names == {"documents", hostile_table, "survivor"}


def test_table_count_rejects_identifier_outside_discovered_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "identifier-allowlist.db"
    hostile_input = 'survivor"; DROP TABLE survivor; --'
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE survivor (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO survivor VALUES (1)")

        with pytest.raises(
            BaselineError,
            match="table count requested for an undiscovered table",
        ):
            sqlite_table_count(
                connection,
                hostile_input,
                known_table_names=frozenset({"survivor"}),
            )

        assert connection.execute("SELECT count(*) FROM survivor").fetchone() == (1,)
