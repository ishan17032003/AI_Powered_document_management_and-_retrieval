"""Contract and regression coverage for the backend retrieval boundary."""

from __future__ import annotations

from sqlalchemy import text

from app.retrieval_store import (
    AuthorizedFilter,
    Fts5RetrievalStore,
    RetrievalChunk,
    RetrievalHit,
    RetrievalStore,
)


def test_authorized_context_manifest_uses_only_returned_chunk_ids() -> None:
    from app.services.rag_service import build_authorized_context_manifest

    chunks = {
        "c-1": RetrievalChunk("c-1", 10, "v1", 0, "allowed text", {"title": "A"}),
        "c-2": RetrievalChunk("c-2", 10, "v1", 1, "secret text", {"title": "A"}),
    }
    hits = [
        RetrievalHit(10, "", 1.0, "lancedb", chunk_id="c-1", version_id="v1"),
        # This hit is not in the final authorization set.
        RetrievalHit(10, "", 0.9, "lancedb", chunk_id="c-2", version_id="v1"),
        # A provider cannot smuggle a document-level result into context.
        RetrievalHit(10, "private", 0.8, "fts5"),
    ]

    manifest = build_authorized_context_manifest(hits, chunks, {"c-1"})

    assert [(item.chunk_id, item.text) for item in manifest] == [("c-1", "allowed text")]
    assert manifest[0].index == 1


def test_authorized_context_manifest_rejects_lineage_mismatch() -> None:
    from app.services.rag_service import build_authorized_context_manifest

    chunk = RetrievalChunk("c-1", 10, "v2", 0, "text")
    hit = RetrievalHit(11, "", 1.0, "lancedb", chunk_id="c-1", version_id="v1")

    assert build_authorized_context_manifest([hit], {"c-1": chunk}, {"c-1"}) == []


def test_multimodal_context_manifest_rechecks_text_and_visual_lineage() -> None:
    from app.services.rag_service import build_multimodal_context_manifest

    chunks = {"c-1": RetrievalChunk("c-1", 11, "v2", 0, "authorized text", {"page": 2})}
    assets = {"asset-1": {"document_id": 11, "version_id": "v2", "asset_type": "PAGE", "page_number": 2, "caption": "diagram"}}
    hits = [RetrievalHit(11, "", 1.0, "lancedb", "c-1", "v2")]
    seen = []
    manifest = build_multimodal_context_manifest(
        hits, chunks, assets, {"c-1"}, {"asset-1"},
        lambda document_id, version_id, asset_id: seen.append((document_id, version_id, asset_id)) is None,
    )
    assert [(item.evidence_type, item.page, item.version_id) for item in manifest] == [("text", 2, "v2"), ("page", 2, "v2")]
    assert seen == [(11, "v2", None), (11, "v2", "asset-1")]


def test_citation_validation_omits_invented_and_mismatched_references() -> None:
    from app.services.rag_service import validate_citations
    from app.utils.rag_types import Passage

    manifest = [
        Passage(1, 10, "A", "text", chunk_id="c-1", version_id="v1", page=3),
    ]
    citations = validate_citations(
        [
            {"index": 1, "document_id": 10, "chunk_id": "c-1", "page": 3},
            {"index": 2, "document_id": 999},
            {"index": 1, "document_id": 11},
            {"index": 1, "document_id": 10, "chunk_id": "forged"},
            {"index": 1, "document_id": 10, "page": 99},
        ],
        manifest,
    )

    assert citations == [{"index": 1, "document_id": 10, "chunk_id": "c-1", "page": 3}]


def test_fts5_adapter_satisfies_backend_contract(db_session) -> None:
    store = Fts5RetrievalStore(db_session)

    assert isinstance(store, RetrievalStore)
    store.ensure_schema("0003")
    assert store.health().ready is True


def test_fts5_adapter_preserves_legacy_ranked_results(db_session) -> None:
    from app.repositories import search_repository
    from app.utils.search_helpers import to_match_query

    # The FTS table is derived data and does not require an ORM document row for
    # this adapter contract test.  Use IDs outside normal seed data.
    search_repository.index_fts(
        db_session,
        7001,
        "Alpha handbook",
        "shared retrieval phrase alpha",
    )
    search_repository.index_fts(
        db_session,
        7002,
        "Beta handbook",
        "shared retrieval phrase beta",
    )
    db_session.flush()

    match_query = to_match_query("shared retrieval phrase")
    reference_rows = db_session.execute(
        text(
            """
            SELECT document_id,
                   snippet(doc_fts, 2, '<mark>', '</mark>', ' … ', 12) AS snippet,
                   bm25(doc_fts) AS score
            FROM doc_fts
            WHERE doc_fts MATCH :q
            ORDER BY score
            LIMIT :lim
            """
        ),
        {"q": match_query, "lim": 60},
    ).fetchall()
    reference = [
        {
            "document_id": document_id,
            "snippet": snippet,
            "score": float(score),
            "source": "fts5",
        }
        for document_id, snippet, score in reference_rows
    ]
    legacy = search_repository.search_fts(db_session, match_query, None, 20)
    adapter = Fts5RetrievalStore(db_session).search(match_query, None, 20)

    assert legacy == reference
    assert legacy == [hit.as_dict() for hit in adapter]
    assert [item["document_id"] for item in legacy] == [7001, 7002]
    assert all(item["source"] == "fts5" for item in legacy)


def test_fts5_adapter_applies_exact_filter_and_empty_set_is_deny_all(
    db_session,
) -> None:
    store = Fts5RetrievalStore(db_session)
    store.upsert_chunks(
        "version-1",
        [
            RetrievalChunk(
                chunk_id="chunk-1",
                document_id=7010,
                version_id="version-1",
                chunk_no=0,
                text="authorized phrase one",
                metadata={"title": "One"},
            ),
            RetrievalChunk(
                chunk_id="chunk-2",
                document_id=7010,
                version_id="version-1",
                chunk_no=1,
                text="authorized phrase two",
            ),
            RetrievalChunk(
                chunk_id="chunk-3",
                document_id=7011,
                version_id="version-1",
                chunk_no=0,
                text="authorized phrase three",
            ),
        ],
    )
    db_session.flush()

    only_one = store.search(
        "authorized AND phrase",
        AuthorizedFilter.from_ids({7010}),
        10,
    )
    assert [hit.document_id for hit in only_one] == [7010]
    assert store.search("authorized", AuthorizedFilter(frozenset()), 10) == []


def test_fts5_index_state_is_conservative_about_versions(db_session) -> None:
    store = Fts5RetrievalStore(db_session)
    store.upsert_document(7020, "State", "state phrase")
    db_session.flush()

    indexed = store.document_index_state(7020, "version-2")
    missing = store.document_index_state(7021, "version-2")

    assert indexed.indexed is True
    assert indexed.indexed_version_id is None
    assert indexed.version_match is None
    assert missing.indexed is False
    assert missing.version_match is False


def test_fts5_reconciliation_reports_coverage_without_guessing_versions(db_session) -> None:
    store = Fts5RetrievalStore(db_session)
    store.upsert_document(7030, "Indexed", "indexed")
    db_session.flush()
    report = store.reconcile({7030: 12, 7031: 13})
    assert report.checked is True
    assert report.indexed_document_ids == frozenset({7030})
    assert report.missing_document_ids == frozenset({7031})
    assert report.version_lag_document_ids == frozenset()


def test_fts5_adapter_delete_and_maintenance_are_explicit(db_session) -> None:
    store = Fts5RetrievalStore(db_session)
    store.upsert_document(7030, "Delete", "delete phrase")
    db_session.flush()
    assert store.search("delete", None, 10)

    store.delete_document(7030)
    db_session.flush()
    assert store.search("delete", None, 10) == []
    maintenance = store.optimize()
    assert maintenance.performed is False
    assert "not_configured" in maintenance.detail
