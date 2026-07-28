"""Feature-flagged retrieval serving, shadow comparison, and rollback boundary."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..config import RetrievalReadMode
from ..observability import emit_event
from ..retrieval_store import AuthorizedFilter, RetrievalHit, RetrievalStore

CurrentSearch = Callable[[], list[dict]]
LanceStore = RetrievalStore | Callable[[], RetrievalStore]


@dataclass(frozen=True, slots=True)
class ShadowQueryReport:
    queries: int
    completed: int
    failed: int
    primary_hits: int
    shadow_hits: int
    overlapping_hits: int
    coverage_ratio: float
    overlap_ratio: float
    average_latency_ms: float
    maximum_latency_ms: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "queries": self.queries,
            "completed": self.completed,
            "failed": self.failed,
            "primary_hits": self.primary_hits,
            "shadow_hits": self.shadow_hits,
            "overlapping_hits": self.overlapping_hits,
            "coverage_ratio": self.coverage_ratio,
            "overlap_ratio": self.overlap_ratio,
            "average_latency_ms": self.average_latency_ms,
            "maximum_latency_ms": self.maximum_latency_ms,
        }


class ShadowQueryMetrics:
    """Bounded process-local aggregate with no query or document content."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self._queries = 0
            self._completed = 0
            self._failed = 0
            self._primary_hits = 0
            self._shadow_hits = 0
            self._overlapping_hits = 0
            self._latency_total_ms = 0.0
            self._latency_max_ms = 0.0

    def record(
        self,
        *,
        primary_document_ids: set[int],
        shadow_document_ids: set[int],
        latency_ms: float,
        failed: bool,
    ) -> None:
        with self._lock:
            self._queries += 1
            self._primary_hits += len(primary_document_ids)
            self._latency_total_ms += max(0.0, latency_ms)
            self._latency_max_ms = max(self._latency_max_ms, latency_ms)
            if failed:
                self._failed += 1
                return
            self._completed += 1
            self._shadow_hits += len(shadow_document_ids)
            self._overlapping_hits += len(
                primary_document_ids & shadow_document_ids
            )

    def snapshot(self) -> ShadowQueryReport:
        with self._lock:
            primary_hits = self._primary_hits
            return ShadowQueryReport(
                queries=self._queries,
                completed=self._completed,
                failed=self._failed,
                primary_hits=primary_hits,
                shadow_hits=self._shadow_hits,
                overlapping_hits=self._overlapping_hits,
                coverage_ratio=round(
                    min(1.0, self._shadow_hits / primary_hits)
                    if primary_hits
                    else 0.0,
                    4,
                ),
                overlap_ratio=round(
                    self._overlapping_hits / primary_hits
                    if primary_hits
                    else 0.0,
                    4,
                ),
                average_latency_ms=round(
                    self._latency_total_ms / self._queries
                    if self._queries
                    else 0.0,
                    3,
                ),
                maximum_latency_ms=round(self._latency_max_ms, 3),
            )


shadow_metrics = ShadowQueryMetrics()


def _document_ids(values: list[dict] | list[RetrievalHit]) -> set[int]:
    identifiers: set[int] = set()
    for value in values:
        candidate = (
            value.document_id
            if isinstance(value, RetrievalHit)
            else value.get("document_id")
        )
        if type(candidate) is int and candidate > 0:
            identifiers.add(candidate)
    return identifiers


def _lance_search(
    store: LanceStore,
    *,
    query: str,
    allowed_ids: set[int] | None,
    limit: int,
) -> list[dict]:
    resolved = store() if callable(store) else store
    authorized_filter = AuthorizedFilter.from_ids(allowed_ids)
    return [
        hit.as_dict()
        for hit in resolved.search(
            query,
            authorized_filter,
            limit,
        )
    ]


def route_search(
    *,
    mode: RetrievalReadMode | str,
    query: str,
    allowed_ids: set[int] | None,
    limit: int,
    current_search: CurrentSearch,
    lancedb_store: LanceStore,
) -> list[dict]:
    """Route one read without mutating either retrieval adapter."""
    selected = RetrievalReadMode(mode)
    if selected is RetrievalReadMode.CURRENT:
        return current_search()

    if selected is RetrievalReadMode.LANCEDB_PRIMARY:
        started = time.perf_counter()
        try:
            results = _lance_search(
                lancedb_store,
                query=query,
                allowed_ids=allowed_ids,
                limit=limit,
            )
        except Exception as exc:
            emit_event(
                "retrieval.primary.completed",
                component="retrieval",
                operation="lancedb_primary",
                outcome="error",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=exc,
            )
            raise
        emit_event(
            "retrieval.primary.completed",
            component="retrieval",
            operation="lancedb_primary",
            outcome="success",
            duration_ms=(time.perf_counter() - started) * 1000,
            count=len(results),
        )
        return results

    primary = current_search()
    started = time.perf_counter()
    try:
        shadow = _lance_search(
            lancedb_store,
            query=query,
            allowed_ids=allowed_ids,
            limit=limit,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        shadow_metrics.record(
            primary_document_ids=_document_ids(primary),
            shadow_document_ids=set(),
            latency_ms=latency_ms,
            failed=True,
        )
        emit_event(
            "retrieval.shadow.completed",
            component="retrieval",
            operation="lancedb_shadow",
            outcome="error",
            duration_ms=latency_ms,
            error=exc,
        )
        return primary

    latency_ms = (time.perf_counter() - started) * 1000
    shadow_metrics.record(
        primary_document_ids=_document_ids(primary),
        shadow_document_ids=_document_ids(shadow),
        latency_ms=latency_ms,
        failed=False,
    )
    emit_event(
        "retrieval.shadow.completed",
        component="retrieval",
        operation="lancedb_shadow",
        outcome="success",
        duration_ms=latency_ms,
        count=len(shadow),
    )
    # Shadow results are deliberately discarded.
    return primary


__all__ = [
    "ShadowQueryMetrics",
    "ShadowQueryReport",
    "route_search",
    "shadow_metrics",
]
