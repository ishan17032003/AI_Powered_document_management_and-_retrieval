"""Persistence adapters for SQLite FTS5 and Qdrant.

Functions in this module deliberately never commit or roll back SQLAlchemy
sessions.  Transaction boundaries remain in the search service.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session, joinedload, selectinload

from .. import models
from ..config import settings
from ..retrieval_store import (
    AuthorizedFilter,
    Fts5RetrievalStore,
    QdrantRetrievalStore,
)
from ..utils.search_helpers import to_match_query

_qdrant_client = None
_qdrant_checked = False
_qdrant_failures = 0
_qdrant_next_probe_at = 0.0
_qdrant_lock = threading.Lock()
_QDRANT_MAX_FAILURES = 5
_QDRANT_BASE_COOLDOWN_SECONDS = 1.0
_QDRANT_MAX_COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True)
class QdrantHealth:
    """Safe, bounded provider state; never includes endpoint or exception text."""

    state: str
    failures: int
    retry_after_seconds: int


def qdrant_health_status() -> QdrantHealth:
    """Return passive circuit state without loading the optional dependency."""
    now = time.monotonic()
    with _qdrant_lock:
        if not settings.use_qdrant:
            state = "disabled"
        elif _qdrant_client is not None:
            state = "ready"
        elif _qdrant_next_probe_at > now:
            state = "cooldown"
        elif _qdrant_checked:
            state = "unavailable"
        else:
            state = "unknown"
        retry = max(0, int(_qdrant_next_probe_at - now))
        return QdrantHealth(state, min(_qdrant_failures, _QDRANT_MAX_FAILURES), retry)


def reset_qdrant_state() -> None:
    """Reset provider state for process lifecycle/tests; does not contact Qdrant."""
    global _qdrant_client, _qdrant_checked, _qdrant_failures, _qdrant_next_probe_at
    with _qdrant_lock:
        _qdrant_client = None
        _qdrant_checked = False
        _qdrant_failures = 0
        _qdrant_next_probe_at = 0.0


def qdrant_is_ready() -> bool:
    """Report cached state without connecting to Qdrant."""
    with _qdrant_lock:
        return settings.use_qdrant and _qdrant_client is not None


def get_qdrant():
    """Return a lazily initialised Qdrant client, or ``None`` when unavailable."""
    global _qdrant_client, _qdrant_checked, _qdrant_failures, _qdrant_next_probe_at
    if not settings.use_qdrant:
        return None
    now = time.monotonic()
    with _qdrant_lock:
        if _qdrant_client is not None:
            return _qdrant_client
        if now < _qdrant_next_probe_at:
            return None
        # Mark before attempting I/O so concurrent requests cannot stampede.
        _qdrant_checked = True
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(url=settings.qdrant_url, timeout=5)
        client.get_collections()
        _ensure_collection(client)
    except Exception:
        with _qdrant_lock:
            _qdrant_client = None
            _qdrant_failures = min(_qdrant_failures + 1, _QDRANT_MAX_FAILURES)
            delay = min(
                _QDRANT_BASE_COOLDOWN_SECONDS * (2 ** (_qdrant_failures - 1)),
                _QDRANT_MAX_COOLDOWN_SECONDS,
            )
            _qdrant_next_probe_at = time.monotonic() + delay
        return None
    with _qdrant_lock:
        _qdrant_client = client
        _qdrant_failures = 0
        _qdrant_next_probe_at = 0.0
        return client


def _ensure_collection(client) -> None:
    try:
        from qdrant_client.models import (
            Distance,
            SparseIndexParams,
            SparseVectorParams,
            VectorParams,
        )

        existing = {
            collection.name for collection in client.get_collections().collections
        }
        if settings.qdrant_collection in existing:
            return
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config={
                "dense": VectorParams(
                    size=settings.qdrant_vector_size,
                    distance=Distance.COSINE,
                ),
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(index=SparseIndexParams(on_disk=False)),
            },
        )
    except Exception:
        pass


def search_qdrant(
    dense_vector: list[float],
    sparse_vector: dict | None,
    allowed_ids: set[int] | None,
    limit: int,
) -> list[dict]:
    """Search Qdrant using vectors prepared by the service layer.

    The legacy service already owns query embedding, so the adapter receives a
    tiny callback that returns those vectors.  Keeping this bridge here avoids
    changing the request-facing search API while moving provider fusion and
    authorization filtering behind ``RetrievalStore``.
    """
    client = get_qdrant()
    if client is None:
        return []
    normalized_sparse: dict[str, list[float]] | None = None
    if sparse_vector:
        normalized_sparse = {
            "indices": [float(value) for value in sparse_vector.get("indices", [])],
            "values": [float(value) for value in sparse_vector.get("values", [])],
        }
    store = QdrantRetrievalStore(
        client,
        collection_name=settings.qdrant_collection,
        embed_query=lambda _query: (dense_vector, normalized_sparse),
    )
    return [
        hit.as_dict()
        for hit in store.search(
            "prepared-vector-query",
            AuthorizedFilter.from_ids(allowed_ids),
            limit,
        )
    ]


def index_qdrant(
    document_id: int,
    title: str,
    snippet: str,
    dense_vector: list[float],
    sparse_vector: dict | None,
) -> bool:
    client = get_qdrant()
    if client is None:
        return False
    try:
        from qdrant_client.models import PointStruct, SparseVector

        vectors: dict = {"dense": dense_vector}
        if sparse_vector:
            vectors["sparse"] = SparseVector(
                indices=sparse_vector["indices"],
                values=sparse_vector["values"],
            )
        client.upsert(
            collection_name=settings.qdrant_collection,
            points=[
                PointStruct(
                    id=document_id,
                    vector=vectors,
                    payload={
                        "document_id": document_id,
                        "title": title,
                        "snippet": snippet,
                    },
                )
            ],
        )
        return True
    except Exception:
        return False


def remove_qdrant(document_id: int) -> None:
    client = get_qdrant()
    if client is None:
        return
    try:
        from qdrant_client.models import PointIdsList

        client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=PointIdsList(points=[document_id]),
        )
    except Exception:
        pass


def search_fts(
    db: Session,
    match_query: str,
    allowed_ids: set[int] | None,
    limit: int,
) -> list[dict]:
    return [
        hit.as_dict()
        for hit in Fts5RetrievalStore(db).search(
            match_query,
            AuthorizedFilter.from_ids(allowed_ids),
            limit,
        )
    ]


def index_fts(db: Session, document_id: int, title: str, content: str) -> None:
    Fts5RetrievalStore(db).upsert_document(document_id, title, content)


def remove_fts(db: Session, document_id: int) -> None:
    Fts5RetrievalStore(db).delete_document(document_id)


# Public, commit-free FTS repository API used by document-oriented services.
def search(
    db: Session,
    query: str,
    allowed_ids: set[int] | None,
    limit: int = 50,
) -> list[dict]:
    return search_fts(db, to_match_query(query), allowed_ids, limit)


def upsert_document(
    db: Session,
    document_id: int,
    title: str,
    content: str,
) -> None:
    index_fts(db, document_id, title, content)


def remove_document(db: Session, document_id: int) -> None:
    remove_fts(db, document_id)


def get_documents(
    db: Session,
    document_ids: set[int],
) -> list[models.Document]:
    if not document_ids:
        return []
    return db.query(models.Document).options(
        joinedload(models.Document.doc_class), selectinload(models.Document.versions)
    ).filter(
        models.Document.id.in_(document_ids),
        models.Document.lifecycle_state == "ACTIVE",
    ).all()
