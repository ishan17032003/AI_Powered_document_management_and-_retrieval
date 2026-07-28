from __future__ import annotations

import pytest

from app.config import RetrievalReadMode
from app.retrieval_store import RetrievalHit
from app.services import retrieval_read_router


class _Store:
    adapter_name = "lancedb"

    def __init__(
        self,
        hits: list[RetrievalHit],
        *,
        fail: bool = False,
    ) -> None:
        self.hits = hits
        self.fail = fail
        self.search_calls = 0
        self.write_calls = 0

    def search(self, query, authorized_filter, limit):
        self.search_calls += 1
        if self.fail:
            raise RuntimeError("provider detail must not be served")
        allowed = authorized_filter.document_ids
        return [
            hit
            for hit in self.hits
            if allowed is None or hit.document_id in allowed
        ][:limit]


def _current() -> list[dict]:
    return [
        {"document_id": 1, "snippet": "current one", "score": 1.0},
        {"document_id": 2, "snippet": "current two", "score": 0.9},
    ]


def test_shadow_queries_never_serve_lancedb_results_and_report_quality() -> None:
    retrieval_read_router.shadow_metrics.reset()
    store = _Store(
        [
            RetrievalHit(1, "shadow one", 0.8, "lancedb", "chunk-1", "v1"),
            RetrievalHit(3, "shadow three", 0.7, "lancedb", "chunk-3", "v1"),
        ]
    )

    served = retrieval_read_router.route_search(
        mode=RetrievalReadMode.LANCEDB_SHADOW,
        query="needle",
        allowed_ids={1, 2, 3},
        limit=10,
        current_search=_current,
        lancedb_store=store,
    )

    assert served == _current()
    report = retrieval_read_router.shadow_metrics.snapshot()
    assert report.queries == 1
    assert report.completed == 1
    assert report.primary_hits == 2
    assert report.shadow_hits == 2
    assert report.overlapping_hits == 1
    assert report.coverage_ratio == 1.0
    assert report.overlap_ratio == 0.5


def test_shadow_failure_preserves_current_results_and_is_counted() -> None:
    retrieval_read_router.shadow_metrics.reset()
    store = _Store([], fail=True)

    served = retrieval_read_router.route_search(
        mode="lancedb_shadow",
        query="needle",
        allowed_ids={1, 2},
        limit=10,
        current_search=_current,
        lancedb_store=store,
    )

    assert served == _current()
    report = retrieval_read_router.shadow_metrics.snapshot()
    assert report.failed == 1
    assert report.completed == 0


def test_primary_cutover_and_immediate_rollback_require_no_writes() -> None:
    store = _Store(
        [RetrievalHit(1, "lance primary", 0.8, "lancedb", "chunk-1", "v1")]
    )

    primary = retrieval_read_router.route_search(
        mode="lancedb_primary",
        query="needle",
        allowed_ids={1},
        limit=10,
        current_search=_current,
        lancedb_store=store,
    )
    rolled_back = retrieval_read_router.route_search(
        mode="current",
        query="needle",
        allowed_ids={1},
        limit=10,
        current_search=_current,
        lancedb_store=store,
    )

    assert primary[0]["source"] == "lancedb"
    assert rolled_back == _current()
    assert store.search_calls == 1
    assert store.write_calls == 0


def test_search_service_consumes_the_runtime_read_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.services import search_service

    store = _Store(
        [RetrievalHit(1, "lance primary", 0.8, "lancedb", "chunk-1", "v1")]
    )
    monkeypatch.setattr(
        settings,
        "retrieval_read_mode",
        RetrievalReadMode.LANCEDB_PRIMARY,
    )
    monkeypatch.setattr(search_service, "_get_lancedb_reader", lambda: store)
    monkeypatch.setattr(
        search_service,
        "_search_current",
        lambda *_args, **_kwargs: _current(),
    )

    results = search_service.search(object(), "needle", {1}, 10)

    assert results[0]["source"] == "lancedb"
    assert search_service.search_status()["retrieval_read_mode"] == "lancedb_primary"
