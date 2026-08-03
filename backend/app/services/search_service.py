"""Hybrid search — FR-IDX-01/02/03.

Strategy (with graceful degradation):
  1. Qdrant hybrid search (BGE-M3 dense + sparse vectors) when Qdrant is reachable
     and the collection has been initialised.
  2. SQLite FTS5 BM25 fallback — same API contract, always available.

Security trimming is applied on every code path: results are always filtered to
document IDs the caller may VIEW before being returned.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from sqlalchemy.orm import Session

from ..chunking import chunk_text
from ..config import RetrievalReadMode, settings
from ..observability import emit_event
from ..repositories import search_repository
from ..retrieval_store import AuthorizedFilter, RetrievalChunk, RetrievalHit, RetrievalStoreError
from ..utils.request_context import bound_request_context, worker_context
from . import lancedb_service, retrieval_read_router


class _SnippetTextParser(HTMLParser):
    """Convert provider/FTS snippet markup to safe plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text_snippet(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _SnippetTextParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()


def _match_ranges(snippet: str, query: str, *, limit: int = 32) -> list[dict[str, int]]:
    """Return bounded, deterministic half-open ranges for query terms.

    Ranges are offsets into the already-normalized plain-text snippet, never
    offsets into provider markup.  Token matching is case-insensitive and
    overlapping ranges are merged so consumers can safely highlight them.
    """
    if not snippet or not query or limit <= 0:
        return []
    terms = list(dict.fromkeys(re.findall(r"[\w]+", query, flags=re.UNICODE)))
    ranges: list[tuple[int, int]] = []
    for term in terms:
        for match in re.finditer(re.escape(term), snippet, flags=re.IGNORECASE):
            ranges.append((match.start(), match.end()))
            if len(ranges) >= limit * 2:
                break
        if len(ranges) >= limit * 2:
            break
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
        if len(merged) >= limit:
            break
    return [{"start": start, "end": end} for start, end in merged]

# ── Lazy model singletons ────────────────────────────────────────────────────
_embed_model = None
_embed_checked = False

_reranker = None
_reranker_checked = False


# ── BGE-M3 embeddings ─────────────────────────────────────────────────────────


def _get_embed_model():
    global _embed_model, _embed_checked
    if _embed_checked:
        return _embed_model
    _embed_checked = True
    if not settings.embedding_model:
        return None
    try:
        from FlagEmbedding import BGEM3FlagModel

        _embed_model = BGEM3FlagModel(settings.embedding_model, use_fp16=True)
    except Exception:
        try:
            from sentence_transformers import SentenceTransformer

            _embed_model = SentenceTransformer(settings.embedding_model)
        except Exception:
            _embed_model = None
    return _embed_model


def _embed(texts: list[str]) -> tuple[list[list[float]], list[dict] | None]:
    """Return (dense_vectors, sparse_vectors_or_None) for a list of texts."""
    model = _get_embed_model()
    if model is None:
        return [], None
    try:
        # BGE-M3 native API.
        output = model.encode(
            texts, return_dense=True, return_sparse=True, return_colbert_vecs=False
        )
        dense = output["dense_vecs"].tolist()
        # Convert sparse {token_id: weight} dicts to Qdrant SparseVector format.
        sparse = []
        for sv in output.get("lexical_weights", [{}]):
            indices = [int(k) for k in sv.keys()]
            values = [float(v) for v in sv.values()]
            sparse.append({"indices": indices, "values": values})
        return dense, sparse
    except Exception:
        try:
            # SentenceTransformer fallback — dense only.
            dense = model.encode(texts, normalize_embeddings=True).tolist()
            return dense, None
        except Exception:
            return [], None


# ── Cross-encoder reranker ────────────────────────────────────────────────────


def _get_reranker():
    global _reranker, _reranker_checked
    if _reranker_checked:
        return _reranker
    _reranker_checked = True
    if not settings.reranker_model:
        return None
    try:
        from FlagEmbedding import FlagReranker

        _reranker = FlagReranker(settings.reranker_model, use_fp16=True)
        # FlagEmbedding constructs fine but can fail only at scoring time
        # (e.g. transformers 5.x removed prepare_for_model). Probe before
        # trusting it so the CrossEncoder fallback actually gets a chance.
        _reranker.compute_score([("probe", "probe")], normalize=True)
    except Exception:
        try:
            from sentence_transformers import CrossEncoder

            _reranker = CrossEncoder(settings.reranker_model)
            _reranker.predict([("probe", "probe")])
        except Exception:
            _reranker = None
    return _reranker


def _rerank(query: str, hits: list[dict], top_k: int | None = None) -> list[dict]:
    """Rerank hits using the cross-encoder; falls back to original order."""
    reranker = _get_reranker()
    if reranker is None or not hits:
        return hits[:top_k] if top_k else hits
    try:
        pairs = [(query, h.get("snippet", "") or h.get("text", "")) for h in hits]
        # BGE FlagReranker API.
        if hasattr(reranker, "compute_score"):
            scores = reranker.compute_score(pairs, normalize=True)
        else:
            # CrossEncoder from sentence-transformers.
            scores = reranker.predict(pairs).tolist()
        for h, s in zip(hits, scores, strict=True):
            h["rerank_score"] = float(s)
        hits.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
    except Exception:
        pass
    return hits[:top_k] if top_k else hits


# ── Qdrant search ─────────────────────────────────────────────────────────────


def _qdrant_search(query: str, allowed_ids: set[int] | None, limit: int) -> list[dict]:
    """Perform hybrid dense+sparse search against Qdrant."""
    if search_repository.get_qdrant() is None:
        return []
    dense_vectors, sparse_vectors = _embed([query])
    if not dense_vectors:
        return []
    sparse_vector = sparse_vectors[0] if sparse_vectors else None
    return search_repository.search_qdrant(
        dense_vectors[0],
        sparse_vector,
        allowed_ids,
        limit,
    )


# ── Qdrant indexing ───────────────────────────────────────────────────────────


def index_vector(document_id: int, title: str, content: str) -> bool:
    """Attempt one Qdrant upsert and report whether it actually completed."""
    if not settings.enable_embeddings:
        return False
    if search_repository.get_qdrant() is None:
        return False
    snippet = (content or "")[:400].strip()
    text_to_embed = f"{title}\n\n{content}"[:2000]
    dense_vectors, sparse_vectors = _embed([text_to_embed])
    if not dense_vectors:
        return False
    sparse_vector = sparse_vectors[0] if sparse_vectors else None
    return search_repository.index_qdrant(
        document_id,
        title,
        snippet,
        dense_vectors[0],
        sparse_vector,
    )


def remove_vector(document_id: int) -> None:
    """Best-effort Qdrant deletion, intended to run after the DB commit."""
    search_repository.remove_qdrant(document_id)


# ── Public API ────────────────────────────────────────────────────────────────


def index_document(db: Session, document_id: int, title: str, content: str) -> None:
    """Index a document in both SQLite FTS5 and Qdrant (if available)."""
    search_repository.upsert_document(db, document_id, title, content)
    db.commit()
    index_vector(document_id, title, content)


def remove_document(db: Session, document_id: int) -> None:
    """Remove a document from both FTS5 and Qdrant."""
    search_repository.remove_document(db, document_id)
    db.commit()
    remove_vector(document_id)


def _search_current(
    db: Session, query: str, allowed_ids: set[int] | None, limit: int = 50
) -> list[dict]:
    """Return ranked hits: [{document_id, snippet, score}]. Trimmed to allowed_ids.

    Uses Qdrant hybrid search when available, with cross-encoder reranking.
    Falls back to SQLite FTS5 BM25 when Qdrant is unavailable.
    """
    # Try Qdrant first.
    hits = _qdrant_search(query, allowed_ids, limit=settings.reranker_top_k)
    if hits:
        # Apply cross-encoder reranking on the Qdrant hits.
        hits = _rerank(query, hits, top_k=limit)
        return [
            {**hit, "snippet": _plain_text_snippet(hit.get("snippet"))}
            for hit in hits
        ]

    # Fall back to FTS5.
    return [
        {**hit, "snippet": _plain_text_snippet(hit.get("snippet"))}
        for hit in search_repository.search(db, query, allowed_ids, limit)
    ]


def _lance_vector_hits(
    query: str, allowed: frozenset[int] | set[int] | None, limit: int
) -> list[dict]:
    """BGE-M3 dense-vector lane over the LanceDB text_chunks table."""
    dense, _ = _embed([query])
    if not dense:
        return []
    try:
        import lancedb

        table = lancedb.connect(str(settings.lancedb_uri)).open_table(
            settings.lancedb_table_name
        )
        builder = table.search(list(dense[0])).distance_type("cosine")
        if allowed is not None:
            values = ",".join(str(value) for value in sorted(allowed))
            builder = builder.where(f"document_id IN ({values})", prefilter=True)
        rows = builder.limit(max(limit * 3, limit)).to_list()
    except Exception:
        return []
    best: dict[int, dict] = {}
    for row in rows:
        document_id = row.get("document_id")
        if type(document_id) is not int or document_id <= 0:
            continue
        if allowed is not None and document_id not in allowed:
            continue
        try:
            score = 1.0 - float(row.get("_distance", 1.0))
        except (TypeError, ValueError):
            continue
        hit = {
            "document_id": document_id,
            "snippet": str(row.get("text") or "")[:400],
            "score": score,
            "source": "lancedb",
            "chunk_id": row.get("chunk_id"),
            "version_id": row.get("version_id"),
        }
        current = best.get(document_id)
        if current is None or score > current["score"]:
            best[document_id] = hit
    return sorted(best.values(), key=lambda item: -item["score"])


class _LanceHybridTextStore:
    """BM25 + BGE-M3 vector lanes over text_chunks, RRF-fused, then reranked
    by the bge-reranker-v2-m3 cross-encoder. Model failures degrade to BM25."""

    adapter_name = "lancedb"
    _RRF_K = 60

    def search(
        self,
        query: str,
        authorized_filter: AuthorizedFilter | None,
        limit: int,
    ) -> list[RetrievalHit]:
        allowed = (
            authorized_filter.document_ids if authorized_filter is not None else None
        )
        if allowed is not None and not allowed:
            return []
        bm25 = [
            hit.as_dict()
            for hit in lancedb_service.reader_store().search(
                query, authorized_filter, limit
            )
        ]
        vector = _lance_vector_hits(query, allowed, limit)
        fused: dict[int, dict] = {}
        snippets: dict[int, list[str]] = {}
        # Vector lane first: its top chunk is the semantically closest text and
        # becomes the document's primary snippet for reranking/display.
        for lane in (vector, bm25):
            for rank, hit in enumerate(lane, start=1):
                document_id = hit["document_id"]
                entry = fused.setdefault(document_id, {**hit, "score": 0.0})
                entry["score"] += 1.0 / (self._RRF_K + rank)
                snippet = str(hit.get("snippet") or "")
                if snippet and snippet not in snippets.setdefault(document_id, []):
                    snippets[document_id].append(snippet)
        candidates = sorted(fused.values(), key=lambda item: -item["score"])
        candidates = candidates[: max(limit * 2, limit)]
        # Each document may have surfaced different chunks per lane; rerank the
        # document by its best-scoring chunk, not by whichever lane came first.
        expanded = [
            {**hit, "snippet": snippet}
            for hit in candidates
            for snippet in (snippets.get(hit["document_id"]) or [""])[:2]
        ]
        best: dict[int, dict] = {}
        for hit in _rerank(query, expanded):
            document_id = hit["document_id"]
            current = best.get(document_id)
            if current is None or hit.get("rerank_score", 0.0) > current.get(
                "rerank_score", 0.0
            ):
                best[document_id] = hit
        ordered = sorted(
            best.values(),
            key=lambda item: -float(item.get("rerank_score", item.get("score", 0.0))),
        )[:limit]
        return [
            RetrievalHit(
                document_id=hit["document_id"],
                snippet=str(hit.get("snippet") or ""),
                score=float(hit.get("rerank_score", hit.get("score", 0.0))),
                source=self.adapter_name,
                chunk_id=hit.get("chunk_id"),
                version_id=hit.get("version_id"),
            )
            for hit in ordered
        ]


def _get_lancedb_reader():
    if settings.lancedb_text_vectors_enabled:
        return _LanceHybridTextStore()
    return lancedb_service.reader_store()


def index_lancedb_chunks(
    document_id: int,
    version_id: int | str,
    title: str,
    content: str,
) -> bool:
    """Best-effort chunk upsert into the LanceDB text lane (single writer)."""
    if not settings.lancedb_writer_enabled:
        return False
    text_value = (content or "").strip()
    if not text_value:
        return False
    try:
        chunks = chunk_text(document_id, version_id, text_value)
        embedding_metadata = None
        if settings.lancedb_text_vectors_enabled and chunks:
            dense, _ = _embed([chunk.text for chunk in chunks])
            if dense and len(dense) == len(chunks):
                embedding_metadata = {
                    "dense_vectors": {
                        chunk.chunk_id: list(vector)
                        for chunk, vector in zip(chunks, dense, strict=True)
                    }
                }
        store = lancedb_service.writer_store()
        store.upsert_chunks(
            str(version_id),
            [
                RetrievalChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=document_id,
                    version_id=str(version_id),
                    chunk_no=chunk.chunk_no,
                    text=chunk.text,
                    metadata={
                        "title": title,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                    },
                )
                for chunk in chunks
            ],
            embedding_metadata,
        )
        return True
    except Exception:
        logging.getLogger(__name__).warning(
            "LanceDB text chunk indexing degraded for document %s", document_id
        )
        return False


def search(
    db: Session, query: str, allowed_ids: set[int] | None, limit: int = 50
) -> list[dict]:
    """Route retrieval according to the reversible serving-mode flag."""
    try:
        return retrieval_read_router.route_search(
            mode=settings.retrieval_read_mode,
            query=query,
            allowed_ids=allowed_ids,
            limit=limit,
            current_search=lambda: _search_current(db, query, allowed_ids, limit),
            lancedb_store=_get_lancedb_reader,
        )
    except RetrievalStoreError:
        # LanceDB-primary must degrade to the current lane (and ultimately to
        # the Postgres OCR passages in RAG) instead of failing the request.
        if RetrievalReadMode(settings.retrieval_read_mode) is RetrievalReadMode.LANCEDB_PRIMARY:
            return _search_current(db, query, allowed_ids, limit)
        raise


def search_with_documents(
    db: Session,
    query: str,
    allowed_ids: set[int] | None,
    limit: int = 50,
) -> tuple[list[dict], list[dict]]:
    """Run retrieval and hydrate the legacy HTTP hit shape in one service call."""
    # Keep the prefilter authoritative even if an index adapter returns a
    # malformed or stale payload.  Hydration is never performed for a hit
    # outside the caller's exact VIEW set.
    hits = [
        hit
        for hit in search(db, query, allowed_ids, limit)
        if (
            type(hit.get("document_id")) is int
            and (allowed_ids is None or hit["document_id"] in allowed_ids)
        )
    ]
    documents = {
        document.id: document
        for document in search_repository.get_documents(
            db,
            {hit["document_id"] for hit in hits},
        )
    }
    hydrated: list[dict] = []
    for hit in hits:
        document = documents.get(hit["document_id"])
        if document is None:
            continue
        hydrated.append(
            {
                "document_id": document.id,
                "title": document.title,
                "doc_class": document.doc_class.name if document.doc_class else None,
                "snippet": _plain_text_snippet(hit.get("snippet")),
                "match_ranges": _match_ranges(
                    _plain_text_snippet(hit.get("snippet")), query
                ),
                "score": hit["score"],
            }
        )
    return hits, hydrated


def search_status() -> dict:
    """Return cached state without loading models or connecting to Qdrant."""

    qdrant_ok = search_repository.qdrant_is_ready()
    embed_ok = _embed_checked and _embed_model is not None
    reranker_ok = _reranker_checked and _reranker is not None
    return {
        "qdrant": qdrant_ok,
        "embedding_model": settings.embedding_model if embed_ok else None,
        "reranker_model": settings.reranker_model if reranker_ok else None,
        "fts5_fallback": True,
        "retrieval_read_mode": settings.retrieval_read_mode.value,
        "lancedb_shadow": retrieval_read_router.shadow_metrics.snapshot().as_dict(),
    }


def warm_models() -> None:
    """Eagerly load embedding + reranker models into RAM.

    Call this from the FastAPI startup event so the first user query
    never waits for a slow model download/load.
    Models are cached in /root/.cache/huggingface (mounted as a named
    Docker volume) so they survive container restarts.
    """
    import threading

    context = worker_context("model-warm")

    def _load() -> None:
        with bound_request_context(context):
            emit_event(
                "worker.model_warm.started",
                context=context,
                component="retrieval",
                operation="embedding",
            )
            try:
                em = _get_embed_model()
                emit_event(
                    "worker.model_warm.completed",
                    context=context,
                    component="retrieval",
                    operation="embedding",
                    outcome="success" if em is not None else "unavailable",
                )
            except Exception as exc:
                emit_event(
                    "worker.model_warm.completed",
                    level=logging.ERROR,
                    context=context,
                    component="retrieval",
                    operation="embedding",
                    outcome="error",
                    error=exc,
                )

            emit_event(
                "worker.model_warm.started",
                context=context,
                component="retrieval",
                operation="reranker",
            )
            try:
                rr = _get_reranker()
                emit_event(
                    "worker.model_warm.completed",
                    context=context,
                    component="retrieval",
                    operation="reranker",
                    outcome="success" if rr is not None else "unavailable",
                )
            except Exception as exc:
                emit_event(
                    "worker.model_warm.completed",
                    level=logging.ERROR,
                    context=context,
                    component="retrieval",
                    operation="reranker",
                    outcome="error",
                    error=exc,
                )

            # After models are loaded, also verify Qdrant connectivity.
            qc = search_repository.get_qdrant()
            emit_event(
                "worker.model_warm.completed",
                context=context,
                component="retrieval",
                operation="vector_store",
                outcome="success" if qc is not None else "unavailable",
            )

    # Run in background thread so the startup event returns immediately
    # and the API is healthy while models are loading.
    t = threading.Thread(target=_load, name="model-warm", daemon=True)
    try:
        t.start()
    except Exception as exc:
        emit_event(
            "worker.model_warm.rejected",
            level=logging.ERROR,
            context=context,
            component="retrieval",
            operation="thread_start",
            outcome="error",
            error=exc,
        )
        raise
