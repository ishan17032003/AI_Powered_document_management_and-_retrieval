"""Dependency-free contract checks for the optional Qdrant adapter."""

from __future__ import annotations

from types import SimpleNamespace

from app.retrieval_store import (
    AuthorizedFilter,
    QdrantRetrievalStore,
    RetrievalStore,
)


class FakeQdrantClient:
    def __init__(self, result_sets: list[list[object]]) -> None:
        self.result_sets = result_sets
        self.requests: list[object] = []
        self.search_calls = 0

    def get_collections(self) -> object:
        return SimpleNamespace(collections=[SimpleNamespace(name="test")])

    def get_collection(self, *, collection_name: str) -> object:
        assert collection_name == "test"
        return SimpleNamespace()

    def search_batch(self, *, collection_name: str, requests: list[object]) -> object:
        assert collection_name == "test"
        self.search_calls += 1
        self.requests = requests
        return self.result_sets


def _point(document_id: int, score: float, snippet: str) -> object:
    return SimpleNamespace(
        id=f"chunk-{document_id}-{score}",
        score=score,
        payload={"document_id": document_id, "snippet": snippet},
    )


def test_qdrant_adapter_satisfies_contract_without_optional_package() -> None:
    client = FakeQdrantClient([])
    store = QdrantRetrievalStore(
        client,
        collection_name="test",
        embed_query=lambda _query: ([0.1, 0.2], None),
    )

    assert isinstance(store, RetrievalStore)
    store.ensure_schema("retrieval-v1")
    assert store.health().ready is True


def test_qdrant_search_prefilters_and_uses_deterministic_rrf() -> None:
    client = FakeQdrantClient(
        [
            [_point(2, 0.99, "dense two"), _point(1, 0.90, "dense one")],
            [
                _point(1, 0.99, "sparse one"),
                _point(2, 0.10, "sparse two"),
                _point(3, 1.0, "hidden three"),
            ],
        ]
    )
    store = QdrantRetrievalStore(
        client,
        collection_name="test",
        embed_query=lambda _query: (
            [0.1, 0.2],
            {"indices": [1], "values": [0.7]},
        ),
    )

    hits = store.search("needle", AuthorizedFilter.from_ids({1, 2}), 10)

    # Document 1 is rank 2 in dense and rank 1 in sparse; document 2 is rank
    # 1 in dense and rank 2 in sparse.  RRF makes the ordering stable without
    # comparing incompatible dense/sparse raw scores.
    assert [hit.document_id for hit in hits] == [1, 2]
    assert hits[0].score > 0
    assert all(hit.source == "qdrant" for hit in hits)
    assert client.search_calls == 1
    assert len(client.requests) == 2
    for request in client.requests:
        filter_value = getattr(request, "filter", None)
        assert filter_value is not None
        if isinstance(filter_value, dict):
            assert filter_value["must"][0]["match"]["any"] == [1, 2]


def test_qdrant_empty_authorization_does_not_call_provider() -> None:
    client = FakeQdrantClient([[_point(1, 1.0, "should not be returned")]])
    store = QdrantRetrievalStore(
        client,
        collection_name="test",
        embed_query=lambda _query: ([0.1], None),
    )

    assert store.search("needle", AuthorizedFilter(frozenset()), 10) == []
    assert client.search_calls == 0
