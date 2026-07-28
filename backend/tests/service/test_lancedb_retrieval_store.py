from __future__ import annotations

import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.retrieval_store import (
    AuthorizedFilter,
    LanceDbRetrievalStore,
    RetrievalChunk,
    RetrievalStoreError,
)


class _Query:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.where_clause: str | None = None
        self.selected: list[str] | None = None
        self.result_limit: int | None = None

    def where(self, clause: str, *, prefilter: bool = True):
        assert prefilter is True
        self.where_clause = clause
        return self

    def select(self, columns: list[str]):
        self.selected = columns
        return self

    def limit(self, value: int):
        self.result_limit = value
        return self

    def to_list(self) -> list[dict]:
        return self.rows[: self.result_limit]


class _Merge:
    def __init__(self, table: "_Table") -> None:
        self.table = table

    def when_matched_update_all(self):
        return self

    def when_not_matched_insert_all(self):
        return self

    def execute(self, rows: list[dict]) -> None:
        self.table.upserts.extend(rows)


class _Table:
    schema = SimpleNamespace(
        names=[
            "chunk_id",
            "document_id",
            "version_id",
            "chunk_no",
            "text",
            "metadata_json",
        ]
    )

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.refreshes = 0
        self.searches: list[_Query] = []
        self.upserts: list[dict] = []
        self.deletes: list[str] = []
        self.optimizations = 0

    def checkout_latest(self) -> None:
        self.refreshes += 1

    def search(self, *_args, **_kwargs) -> _Query:
        query = _Query(self.rows)
        self.searches.append(query)
        return query

    def merge_insert(self, column: str) -> _Merge:
        assert column == "chunk_id"
        return _Merge(self)

    def delete(self, clause: str) -> None:
        self.deletes.append(clause)

    def optimize(self) -> None:
        self.optimizations += 1


def _write_from_second_process(uri: str, lock_path: str) -> None:
    store = LanceDbRetrievalStore(
        uri,
        writer=True,
        lock_path=lock_path,
        lock_timeout_seconds=2,
    )
    store.upsert_chunks(
        "v2",
        [RetrievalChunk("chunk-v2", 902, "v2", 0, "visible after refresh")],
    )


def test_lancedb_is_lazy_and_fails_closed_without_optional_state() -> None:
    store = LanceDbRetrievalStore()
    assert store.health().ready is False
    with pytest.raises(RetrievalStoreError, match="unavailable"):
        store.ensure_schema("1")


def test_reader_refreshes_and_enforces_exact_authorization_filter() -> None:
    table = _Table(
        [
            {
                "chunk_id": "allowed",
                "document_id": 11,
                "version_id": "v1",
                "text": "allowed result",
                "_score": 0.9,
            },
            {
                "chunk_id": "blocked",
                "document_id": 12,
                "version_id": "v1",
                "text": "must not escape",
                "_score": 1.0,
            },
        ]
    )
    store = LanceDbRetrievalStore(table=table)

    hits = store.search("result", AuthorizedFilter.from_ids({11}), 10)

    assert [hit.document_id for hit in hits] == [11]
    assert table.refreshes == 1
    assert table.searches[0].where_clause == "document_id IN (11)"


def test_writer_role_and_shared_lock_cover_writes_and_maintenance(
    tmp_path: Path,
) -> None:
    table = _Table()
    reader = LanceDbRetrievalStore(table=table)
    with pytest.raises(RetrievalStoreError, match="read-only"):
        reader.upsert_chunks(
            "v1",
            [RetrievalChunk("chunk-1", 1, "v1", 0, "text")],
        )

    lock_path = tmp_path / "writer.lock"
    writer = LanceDbRetrievalStore(
        table=table,
        writer=True,
        lock_path=lock_path,
        lock_timeout_seconds=0.05,
    )
    writer.upsert_chunks(
        "v1",
        [
            RetrievalChunk(
                "chunk-1",
                1,
                "v1",
                0,
                "text",
                {"page": 1},
            )
        ],
    )
    assert table.upserts[0]["metadata_json"] == '{"page":1}'
    assert writer.optimize().performed is True
    assert table.optimizations == 1


    portalocker = pytest.importorskip("portalocker")
    with portalocker.Lock(
        lock_path,
        mode="a",
        timeout=0,
        flags=(
            portalocker.LockFlags.EXCLUSIVE
            | portalocker.LockFlags.NON_BLOCKING
        ),
    ):
        with pytest.raises(RetrievalStoreError, match="lock"):
            writer.optimize()
    assert table.optimizations == 1


def test_reader_refresh_is_non_mutating_and_maintenance_lock_is_writer_only(
    tmp_path: Path,
) -> None:
    pytest.importorskip("portalocker")
    table = _Table()
    reader = LanceDbRetrievalStore(table=table)
    reader.refresh()
    assert table.refreshes == 1
    with pytest.raises(RetrievalStoreError, match="read-only"):
        with reader.maintenance_lock():
            pass
    writer = LanceDbRetrievalStore(
        table=table, writer=True, lock_path=tmp_path / "maintenance.lock"
    )
    with writer.maintenance_lock():
        assert table.refreshes == 1


def test_second_process_write_becomes_visible_after_reader_refresh(
    tmp_path: Path,
) -> None:
    pytest.importorskip("lancedb")
    uri = tmp_path / "lancedb"
    lock_path = tmp_path / "writer.lock"
    LanceDbRetrievalStore.provision(
        uri,
        lock_path=lock_path,
        lock_timeout_seconds=2,
    )
    reader = LanceDbRetrievalStore(uri)
    assert reader.document_index_state(902, "v2").indexed is False

    process = multiprocessing.get_context("spawn").Process(
        target=_write_from_second_process,
        args=(str(uri), str(lock_path)),
    )
    process.start()
    process.join(timeout=20)

    assert process.exitcode == 0
    state = reader.document_index_state(902, "v2")
    assert state.indexed is True
    assert state.version_match is True


def test_provisioned_fts_search_returns_authorized_chunk_lineage(
    tmp_path: Path,
) -> None:
    pytest.importorskip("lancedb")
    uri = tmp_path / "lancedb-search"
    lock_path = tmp_path / "search-writer.lock"
    writer = LanceDbRetrievalStore.provision(uri, lock_path=lock_path)
    writer.upsert_chunks(
        "v3",
        [
            RetrievalChunk("visible-chunk", 903, "v3", 0, "distinctive visible phrase"),
            RetrievalChunk("hidden-chunk", 904, "v3", 0, "distinctive visible phrase"),
        ],
    )

    reader = LanceDbRetrievalStore(uri)
    hits = reader.search(
        "distinctive visible",
        AuthorizedFilter.from_ids({903}),
        10,
    )

    assert [(hit.document_id, hit.chunk_id, hit.version_id) for hit in hits] == [
        (903, "visible-chunk", "v3")
    ]
