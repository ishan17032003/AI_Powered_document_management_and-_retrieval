"""Backend-owned retrieval contract and the local SQLite FTS5 adapter.

The relational database and object store remain authoritative.  Retrieval data
is an accelerator that may be deleted and rebuilt by an explicit orchestrator.
The optional Qdrant dependency is imported only inside the Qdrant adapter's
operations.  LanceDB remains a separate, not-yet-implemented adapter and the
request-facing search service depends only on this protocol.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.orm import Session


class RetrievalStoreError(RuntimeError):
    """Raised when a retrieval adapter cannot satisfy its local contract."""


@dataclass(frozen=True, slots=True)
class AuthorizedFilter:
    """Exact SQL-authorized document IDs supplied to a retrieval adapter.

    ``None`` means that the caller has intentionally chosen an unscoped query
    (for example, an administrative maintenance operation).  An empty set is
    different: it means the caller is authorized to see no documents and must
    return no hits.
    """

    document_ids: frozenset[int] | None = None

    @classmethod
    def from_ids(cls, document_ids: Collection[int] | None) -> "AuthorizedFilter":
        if document_ids is None:
            return cls(None)
        normalized = frozenset(
            value for value in document_ids if type(value) is int and value > 0
        )
        return cls(normalized)


@dataclass(frozen=True, slots=True)
class RetrievalChunk:
    """Minimal typed chunk envelope shared by future retrieval adapters."""

    chunk_id: str
    document_id: int
    version_id: str | int
    chunk_no: int
    text: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """Stable result shape returned by all adapters."""

    document_id: int
    snippet: str
    score: float
    source: str
    # Chunk identity is optional for legacy document-level adapters (FTS5).
    # Chunk-aware adapters must return it so callers can construct an exact
    # authorization-bound context manifest.
    chunk_id: str | None = None
    version_id: str | int | None = None

    def as_dict(self) -> dict[str, object]:
        result = {
            "document_id": self.document_id,
            "snippet": self.snippet,
            "score": self.score,
            "source": self.source,
        }
        if self.chunk_id is not None:
            result["chunk_id"] = self.chunk_id
        if self.version_id is not None:
            result["version_id"] = self.version_id
        return result


@dataclass(frozen=True, slots=True)
class DocumentIndexState:
    """Index presence and version knowledge for one authoritative document."""

    document_id: int
    indexed: bool
    requested_version_id: str | int | None
    indexed_version_id: str | int | None
    version_match: bool | None


@dataclass(frozen=True, slots=True)
class RetrievalHealth:
    """Bounded local health result with no provider exception details."""

    adapter: str
    ready: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RetrievalMaintenance:
    """Result of an explicit adapter maintenance request."""

    adapter: str
    performed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RetrievalReconciliation:
    """Bounded adapter-owned reconciliation result."""

    adapter: str
    checked: bool
    indexed_document_ids: frozenset[int] = frozenset()
    missing_document_ids: frozenset[int] = frozenset()
    stale_document_ids: frozenset[int] = frozenset()
    version_lag_document_ids: frozenset[int] = frozenset()
    detail: str = ""


@runtime_checkable
class RetrievalStore(Protocol):
    """Common contract for text, page, and image retrieval adapters.

    Implementations must not commit or roll back a caller-owned relational
    transaction.  ``rebuild`` is intentionally absent: rebuilding is an
    explicit, resumable orchestration job rather than a startup side effect.
    """

    adapter_name: str

    def ensure_schema(self, schema_version: str | int) -> None:
        """Validate adapter-owned structures without creating or repairing them."""

    def upsert_chunks(
        self,
        document_version: str | int,
        chunks: Sequence[RetrievalChunk],
        embedding_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Insert or replace all chunks for one authoritative document version."""

    def delete_document(self, document_id: int) -> None:
        """Remove all derived rows for a document; caller owns the transaction."""

    def search(
        self,
        query: str,
        authorized_filter: AuthorizedFilter | None,
        limit: int,
    ) -> list[RetrievalHit]:
        """Return ranked, already-prefiltered retrieval hits."""

    def document_index_state(
        self,
        document_id: int,
        version_id: str | int | None = None,
    ) -> DocumentIndexState:
        """Report presence and version knowledge for a document."""

    def health(self) -> RetrievalHealth:
        """Run a bounded local readiness check."""

    def optimize(self) -> RetrievalMaintenance:
        """Perform adapter maintenance only when explicitly requested."""

    def reconcile(
        self,
        authoritative_versions: Mapping[int, str | int | None],
        *,
        max_items: int = 10_000,
    ) -> RetrievalReconciliation:
        """Compare adapter coverage with authoritative document/version IDs."""


class Fts5RetrievalStore:
    """Temporary local adapter over the existing SQLite ``doc_fts`` table.

    FTS5 stores one row per document in this application, so chunk upserts are
    collapsed into one document row.  Version/chunk lineage is intentionally
    reported as unknown instead of being guessed; the richer LanceDB contract
    will provide that metadata when its adapter is implemented.
    """

    adapter_name = "fts5"

    def __init__(self, db: Session) -> None:
        self.db = db
        self._is_sqlite = db.bind is not None and db.bind.dialect.name == "sqlite"

    def ensure_schema(self, schema_version: str | int) -> None:
        del schema_version
        if not self._is_sqlite:
            return
        row = self.db.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'doc_fts' LIMIT 1"
            )
        ).scalar_one_or_none()
        if row != 1:
            raise RetrievalStoreError("FTS5 retrieval schema is unavailable")

    def upsert_chunks(
        self,
        document_version: str | int,
        chunks: Sequence[RetrievalChunk],
        embedding_metadata: Mapping[str, object] | None = None,
    ) -> None:
        if not self._is_sqlite:
            return
        del document_version, embedding_metadata
        grouped: dict[int, list[RetrievalChunk]] = defaultdict(list)
        for chunk in chunks:
            if type(chunk.document_id) is not int or chunk.document_id <= 0:
                raise ValueError("chunk document_id must be a positive integer")
            grouped[chunk.document_id].append(chunk)
        for document_id, document_chunks in grouped.items():
            title = ""
            texts: list[str] = []
            for chunk in sorted(document_chunks, key=lambda item: item.chunk_no):
                candidate = chunk.metadata.get("title")
                if not title and isinstance(candidate, str):
                    title = candidate
                texts.append(chunk.text or "")
            self.upsert_document(document_id, title, "\n\n".join(texts))

    def upsert_document(self, document_id: int, title: str, content: str) -> None:
        if not self._is_sqlite:
            return
        self.db.execute(
            text("DELETE FROM doc_fts WHERE document_id = :id"), {"id": document_id}
        )
        self.db.execute(
            text(
                "INSERT INTO doc_fts (document_id, title, content) VALUES (:id, :t, :c)"
            ),
            {"id": document_id, "t": title or "", "c": content or ""},
        )

    def delete_document(self, document_id: int) -> None:
        if not self._is_sqlite:
            return
        self.db.execute(
            text("DELETE FROM doc_fts WHERE document_id = :id"), {"id": document_id}
        )

    def search(
        self,
        query: str,
        authorized_filter: AuthorizedFilter | None,
        limit: int,
    ) -> list[RetrievalHit]:
        if not self._is_sqlite:
            return []
        if not query or limit <= 0:
            return []
        rows = self.db.execute(
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
            {"q": query, "lim": limit * 3},
        ).fetchall()
        allowed_ids = (
            authorized_filter.document_ids if authorized_filter is not None else None
        )
        hits: list[RetrievalHit] = []
        for document_id, snippet, score in rows:
            if allowed_ids is not None and document_id not in allowed_ids:
                continue
            hits.append(
                RetrievalHit(
                    document_id=document_id,
                    snippet=snippet,
                    score=float(score),
                    source=self.adapter_name,
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def document_index_state(
        self,
        document_id: int,
        version_id: str | int | None = None,
    ) -> DocumentIndexState:
        if not self._is_sqlite:
            return DocumentIndexState(document_id, False, version_id, None, False)
        indexed = (
            self.db.execute(
                text("SELECT 1 FROM doc_fts WHERE document_id = :id LIMIT 1"),
                {"id": document_id},
            ).scalar_one_or_none()
            == 1
        )
        return DocumentIndexState(
            document_id=document_id,
            indexed=indexed,
            requested_version_id=version_id,
            indexed_version_id=None,
            version_match=None if indexed else False,
        )

    def health(self) -> RetrievalHealth:
        if not self._is_sqlite:
            return RetrievalHealth(self.adapter_name, False, "unavailable_on_postgres")
        try:
            self.ensure_schema("legacy")
            self.db.execute(text("SELECT 1 FROM doc_fts LIMIT 1")).scalar()
        except Exception:
            return RetrievalHealth(self.adapter_name, False, "schema_or_query_failed")
        return RetrievalHealth(self.adapter_name, True, "ready")

    def optimize(self) -> RetrievalMaintenance:
        # FTS5's optimize command is a mutating operation.  Keep maintenance
        # explicit and report that this migration adapter has no scheduled
        # maintenance policy yet instead of silently mutating a request session.
        return RetrievalMaintenance(
            self.adapter_name, False, "explicit_maintenance_not_configured"
        )

    def reconcile(
        self,
        authoritative_versions: Mapping[int, str | int | None],
        *,
        max_items: int = 10_000,
    ) -> RetrievalReconciliation:
        if not self._is_sqlite:
            return RetrievalReconciliation(self.adapter_name, False, detail="unavailable on postgres")
        if type(max_items) is not int or not 1 <= max_items <= 100_000:
            raise ValueError("max_items must be between 1 and 100000")
        rows = self.db.execute(text("SELECT document_id FROM doc_fts")).scalars().all()
        indexed = {int(value) for value in rows if type(value) is int}
        expected = set(authoritative_versions)
        return RetrievalReconciliation(
            adapter=self.adapter_name,
            checked=True,
            indexed_document_ids=frozenset(sorted(indexed)[:max_items]),
            missing_document_ids=frozenset(sorted(expected - indexed)[:max_items]),
            stale_document_ids=frozenset(sorted(indexed - expected)[:max_items]),
            detail="FTS5 does not retain authoritative version lineage",
        )


class QdrantRetrievalStore:
    """Optional Qdrant adapter for the backend-owned retrieval contract.

    The Qdrant Python package and client are deliberately imported/resolved only
    when an operation is requested.  Constructing this adapter is therefore
    safe in the lightweight SQLite profile and in tests that do not install the
    optional ``ai`` dependencies.  The relational database remains the source
    of authorization truth: an exact ``AuthorizedFilter`` is sent to Qdrant
    where possible and every returned payload is checked again locally.

    Qdrant's dense and sparse result lists are fused by reciprocal rank (RRF),
    not by comparing incompatible raw scores.  Results are aggregated to one
    deterministic hit per document; ties are ordered by document ID.
    """

    adapter_name = "qdrant"
    _RRF_K = 60

    def __init__(
        self,
        client: object | None = None,
        *,
        collection_name: str | None = None,
        embed_query: Callable[[str], tuple[list[float], dict[str, list[float]] | None]]
        | None = None,
    ) -> None:
        self._client = client
        self._collection_name = collection_name
        self._embed_query = embed_query
        self._client_resolved = client is not None

    @property
    def _name(self) -> str:
        if self._collection_name:
            return self._collection_name
        # Import settings lazily so importing this adapter cannot trigger
        # provider configuration or optional dependency loading.
        from .config import settings

        return settings.qdrant_collection

    def _resolve_client(self) -> object | None:
        if self._client_resolved:
            return self._client
        self._client_resolved = True
        try:
            # Reuse the existing guarded/lazy client factory.  Importing the
            # repository here avoids a module-level retrieval dependency cycle.
            from .repositories.search_repository import get_qdrant

            self._client = get_qdrant()
        except Exception:
            self._client = None
        return self._client

    def _encode_query(
        self, query: str
    ) -> tuple[list[float], dict[str, list[float]] | None]:
        if self._embed_query is not None:
            dense, sparse = self._embed_query(query)
            return list(dense), sparse
        try:
            # The existing BGE-M3/SentenceTransformer encoder is itself lazy.
            from .services.search_service import _embed

            dense_vectors, sparse_vectors = _embed([query])
            sparse = sparse_vectors[0] if sparse_vectors else None
            return (list(dense_vectors[0]), sparse) if dense_vectors else ([], sparse)
        except Exception:
            return [], None

    @staticmethod
    def _filter_for_ids(authorized_filter: AuthorizedFilter | None) -> object | None:
        if authorized_filter is None or authorized_filter.document_ids is None:
            return None
        ids = sorted(authorized_filter.document_ids)
        if not ids:
            return None
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchAny

            return Filter(
                must=[FieldCondition(key="document_id", match=MatchAny(any=ids))]
            )
        except Exception:
            # This shape is accepted by Qdrant's JSON-compatible clients and
            # gives dependency-free fakes an inspectable authorization filter.
            return {"must": [{"key": "document_id", "match": {"any": ids}}]}

    @staticmethod
    def _vector(name: str, values: list[float]) -> object:
        try:
            from qdrant_client.models import NamedVector

            return NamedVector(name=name, vector=values)
        except Exception:
            return SimpleNamespace(name=name, vector=values)

    @staticmethod
    def _sparse_vector(values: dict[str, list[float]]) -> object:
        indices = [int(value) for value in values.get("indices", [])]
        weights = [float(value) for value in values.get("values", [])]
        try:
            from qdrant_client.models import NamedSparseVector, SparseVector

            return NamedSparseVector(
                name="sparse",
                vector=SparseVector(indices=indices, values=weights),
            )
        except Exception:
            return SimpleNamespace(
                name="sparse",
                vector=SimpleNamespace(indices=indices, values=weights),
            )

    @classmethod
    def _search_request(
        cls,
        vector: object,
        limit: int,
        filter_value: object | None,
    ) -> object:
        kwargs: dict[str, object] = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
        }
        if filter_value is not None:
            kwargs["filter"] = filter_value
        try:
            from qdrant_client.models import SearchRequest

            return SearchRequest(**kwargs)
        except Exception:
            return SimpleNamespace(**kwargs)

    @staticmethod
    def _result_value(result: object, name: str, default: object = None) -> object:
        if isinstance(result, Mapping):
            return result.get(name, default)
        return getattr(result, name, default)

    @classmethod
    def _result_score(cls, result: object) -> float:
        value = cls._result_value(result, "score", 0.0)
        if isinstance(value, (int, float, str)):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0

    @classmethod
    def _ordered_results(cls, values: object) -> list[object]:
        if not isinstance(values, (list, tuple)):
            return []
        # Qdrant normally returns each lane pre-ranked.  Sorting here makes
        # fake-client and tie behavior deterministic without trusting provider
        # insertion order.
        return sorted(
            values,
            key=lambda result: (
                -cls._result_score(result),
                str(cls._result_value(result, "id", "")),
            ),
        )

    def ensure_schema(self, schema_version: str | int) -> None:
        if schema_version in ("", None):
            raise ValueError("Qdrant schema version is required")
        client = self._resolve_client()
        if client is None:
            raise RetrievalStoreError("Qdrant retrieval client is unavailable")
        try:
            collections = client.get_collections()  # type: ignore[attr-defined]
            names = {
                str(getattr(item, "name", ""))
                for item in getattr(collections, "collections", [])
            }
        except Exception as exc:
            del exc
            raise RetrievalStoreError(
                "Qdrant retrieval schema is unavailable"
            ) from None
        if self._name not in names:
            raise RetrievalStoreError("Qdrant retrieval schema is unavailable")

    def upsert_chunks(
        self,
        document_version: str | int,
        chunks: Sequence[RetrievalChunk],
        embedding_metadata: Mapping[str, object] | None = None,
    ) -> None:
        del document_version
        client = self._resolve_client()
        if client is None:
            raise RetrievalStoreError("Qdrant retrieval client is unavailable")
        metadata = embedding_metadata or {}
        dense_vectors = metadata.get("dense_vectors", {})
        sparse_vectors = metadata.get("sparse_vectors", {})
        if not isinstance(dense_vectors, Mapping):
            raise ValueError("dense_vectors must be keyed by chunk_id")
        if not isinstance(sparse_vectors, Mapping):
            raise ValueError("sparse_vectors must be keyed by chunk_id")

        points: list[object] = []
        for chunk in chunks:
            dense = dense_vectors.get(
                chunk.chunk_id, chunk.metadata.get("dense_vector")
            )
            if not isinstance(dense, (list, tuple)) or not dense:
                raise ValueError("each Qdrant chunk requires a dense vector")
            sparse_raw = sparse_vectors.get(
                chunk.chunk_id, chunk.metadata.get("sparse_vector")
            )
            vectors: dict[str, object] = {"dense": [float(value) for value in dense]}
            if isinstance(sparse_raw, Mapping):
                vectors["sparse"] = self._sparse_vector(
                    {
                        "indices": list(sparse_raw.get("indices", [])),
                        "values": list(sparse_raw.get("values", [])),
                    }
                ).vector  # type: ignore[attr-defined]
            payload = {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "version_id": chunk.version_id,
                "chunk_no": chunk.chunk_no,
                "snippet": chunk.text[:400],
                **{
                    str(key): value
                    for key, value in chunk.metadata.items()
                    if key not in {"dense_vector", "sparse_vector"}
                },
            }
            try:
                from qdrant_client.models import PointStruct

                points.append(
                    PointStruct(id=chunk.chunk_id, vector=vectors, payload=payload)
                )
            except Exception:
                points.append(
                    SimpleNamespace(id=chunk.chunk_id, vector=vectors, payload=payload)
                )
        if not points:
            return
        try:
            client.upsert(  # type: ignore[attr-defined]
                collection_name=self._name,
                points=points,
                wait=True,
            )
        except Exception as exc:
            del exc
            raise RetrievalStoreError("Qdrant chunk upsert failed") from None

    def delete_document(self, document_id: int) -> None:
        if type(document_id) is not int or document_id <= 0:
            raise ValueError("document_id must be a positive integer")
        client = self._resolve_client()
        if client is None:
            raise RetrievalStoreError("Qdrant retrieval client is unavailable")
        filter_value = self._filter_for_ids(AuthorizedFilter.from_ids({document_id}))
        try:
            from qdrant_client.models import FilterSelector

            selector: object = FilterSelector(filter=filter_value)
        except Exception:
            selector = SimpleNamespace(filter=filter_value)
        try:
            client.delete(  # type: ignore[attr-defined]
                collection_name=self._name,
                points_selector=selector,
                wait=True,
            )
        except Exception as exc:
            del exc
            raise RetrievalStoreError("Qdrant document delete failed") from None

    def search(
        self,
        query: str,
        authorized_filter: AuthorizedFilter | None,
        limit: int,
    ) -> list[RetrievalHit]:
        if not query or limit <= 0:
            return []
        if (
            authorized_filter is not None
            and authorized_filter.document_ids is not None
            and not authorized_filter.document_ids
        ):
            return []
        client = self._resolve_client()
        if client is None:
            return []
        dense, sparse = self._encode_query(query)
        if not dense:
            return []
        filter_value = self._filter_for_ids(authorized_filter)
        requests = [
            self._search_request(self._vector("dense", dense), limit * 3, filter_value)
        ]
        if sparse and sparse.get("indices") and sparse.get("values"):
            requests.append(
                self._search_request(
                    self._sparse_vector(sparse), limit * 3, filter_value
                )
            )
        try:
            result_sets = client.search_batch(  # type: ignore[attr-defined]
                collection_name=self._name,
                requests=requests,
            )
        except Exception as exc:
            del exc
            return []

        # Aggregate each lane by document and apply reciprocal-rank fusion.
        # The final local authorization check remains mandatory even when the
        # provider accepted the prefilter.
        fused: dict[int, float] = defaultdict(float)
        best: dict[int, tuple[float, str, str]] = {}
        for lane_results in result_sets or []:
            seen_documents: set[int] = set()
            rank = 0
            for result in self._ordered_results(lane_results):
                payload = self._result_value(result, "payload", {})
                if not isinstance(payload, Mapping):
                    continue
                document_id = payload.get("document_id")
                if type(document_id) is not int or document_id <= 0:
                    continue
                if (
                    authorized_filter is not None
                    and authorized_filter.document_ids is not None
                    and document_id not in authorized_filter.document_ids
                ):
                    continue
                if document_id in seen_documents:
                    continue
                seen_documents.add(document_id)
                # Count only candidates that survive the exact local check.
                # A stale/malicious provider payload must not perturb the
                # rank-fusion score of authorized documents.
                rank += 1
                contribution = 1.0 / (self._RRF_K + rank)
                fused[document_id] += contribution
                provider_score = self._result_score(result)
                chunk_id = str(
                    payload.get("chunk_id", self._result_value(result, "id", ""))
                )
                previous = best.get(document_id)
                candidate = (provider_score, chunk_id, str(payload.get("snippet", "")))
                if previous is None or (provider_score, chunk_id) > (
                    previous[0],
                    previous[1],
                ):
                    best[document_id] = candidate

        ordered_documents = sorted(
            fused,
            key=lambda document_id: (-fused[document_id], document_id),
        )[:limit]
        return [
            RetrievalHit(
                document_id=document_id,
                snippet=best[document_id][2],
                score=fused[document_id],
                source=self.adapter_name,
                chunk_id=best[document_id][1] or None,
            )
            for document_id in ordered_documents
            if document_id in best
        ]

    def document_index_state(
        self,
        document_id: int,
        version_id: str | int | None = None,
    ) -> DocumentIndexState:
        if type(document_id) is not int or document_id <= 0:
            raise ValueError("document_id must be a positive integer")
        client = self._resolve_client()
        if client is None:
            return DocumentIndexState(document_id, False, version_id, None, False)
        filter_value = self._filter_for_ids(AuthorizedFilter.from_ids({document_id}))
        try:
            points, _ = client.scroll(  # type: ignore[attr-defined]
                collection_name=self._name,
                scroll_filter=filter_value,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return DocumentIndexState(document_id, False, version_id, None, False)
        indexed_versions = {
            point.payload.get("version_id")
            for point in points or []
            if isinstance(getattr(point, "payload", None), Mapping)
        }
        indexed = bool(indexed_versions)
        indexed_version: str | int | None = None
        if len(indexed_versions) == 1:
            indexed_version = next(iter(indexed_versions))
        return DocumentIndexState(
            document_id,
            indexed,
            version_id,
            indexed_version,
            indexed and version_id is not None and indexed_versions == {version_id},
        )

    def health(self) -> RetrievalHealth:
        client = self._resolve_client()
        if client is None:
            return RetrievalHealth(self.adapter_name, False, "client_unavailable")
        try:
            client.get_collection(collection_name=self._name)  # type: ignore[attr-defined]
        except Exception:
            return RetrievalHealth(self.adapter_name, False, "collection_unavailable")
        return RetrievalHealth(self.adapter_name, True, "ready")

    def optimize(self) -> RetrievalMaintenance:
        return RetrievalMaintenance(
            self.adapter_name, False, "explicit_maintenance_not_configured"
        )

    def reconcile(
        self,
        authoritative_versions: Mapping[int, str | int | None],
        *,
        max_items: int = 10_000,
    ) -> RetrievalReconciliation:
        del authoritative_versions
        if type(max_items) is not int or not 1 <= max_items <= 100_000:
            raise ValueError("max_items must be between 1 and 100000")
        return RetrievalReconciliation(
            self.adapter_name, False, detail="provider reconciliation is unavailable"
        )


class LanceDbRetrievalStore:
    """Optional LanceDB adapter for the locked local/single-writer profile.

    Readers refresh their table checkout before every observation so a
    long-lived API process sees versions committed by the dedicated writer.
    Every mutation and maintenance operation requires ``writer=True`` and the
    same cross-process file lock. SQL/object storage remain authoritative.
    """

    adapter_name = "lancedb"
    _REQUIRED_COLUMNS = frozenset(
        {
            "chunk_id",
            "document_id",
            "version_id",
            "chunk_no",
            "text",
            "metadata_json",
        }
    )

    def __init__(
        self,
        uri: str | Path | None = None,
        *,
        table_name: str = "text_chunks",
        table: object | None = None,
        writer: bool = False,
        lock_path: str | Path | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        if not table_name or len(table_name) > 64:
            raise ValueError("LanceDB table name is invalid")
        if not 0.05 <= float(lock_timeout_seconds) <= 300.0:
            raise ValueError("LanceDB lock timeout must be between 0.05 and 300 seconds")
        self._uri = str(uri) if uri is not None else None
        self._table_name = table_name
        self._table = table
        self._resolved = table is not None
        self._writer = writer
        self._lock_timeout_seconds = float(lock_timeout_seconds)
        if lock_path is not None:
            self._lock_path = Path(lock_path)
        elif uri is not None:
            self._lock_path = Path(uri) / ".docvault-lancedb-writer.lock"
        else:
            self._lock_path = None

    @classmethod
    def provision(
        cls,
        uri: str | Path,
        *,
        table_name: str = "text_chunks",
        vector_dimensions: int | None = None,
        lock_path: str | Path | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> "LanceDbRetrievalStore":
        """Explicitly create the reviewed schema and its FTS/scalar indexes."""
        if vector_dimensions is not None and not 1 <= vector_dimensions <= 65_536:
            raise ValueError("vector_dimensions must be between 1 and 65536")
        store = cls(
            uri,
            table_name=table_name,
            writer=True,
            lock_path=lock_path,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        with store._exclusive_lock():
            try:
                import lancedb
                import pyarrow as pa

                fields = [
                    pa.field("chunk_id", pa.string(), nullable=False),
                    pa.field("document_id", pa.int64(), nullable=False),
                    pa.field("version_id", pa.string(), nullable=False),
                    pa.field("chunk_no", pa.int32(), nullable=False),
                    pa.field("text", pa.string(), nullable=False),
                    pa.field("metadata_json", pa.string(), nullable=False),
                ]
                if vector_dimensions is not None:
                    fields.append(
                        pa.field(
                            "vector",
                            pa.list_(pa.float32(), vector_dimensions),
                            nullable=True,
                        )
                    )
                database = lancedb.connect(str(uri))
                table = database.create_table(
                    table_name,
                    schema=pa.schema(fields),
                    exist_ok=True,
                )
                table.create_fts_index("text", replace=True, use_tantivy=False)
                table.create_scalar_index("document_id", replace=True)
                table.create_scalar_index("version_id", replace=True)
                store._table = table
                store._resolved = True
            except Exception as exc:
                raise RetrievalStoreError(
                    "LanceDB schema provisioning failed"
                ) from exc
        return store

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        if not self._writer:
            raise RetrievalStoreError("LanceDB adapter is configured read-only")
        if self._lock_path is None:
            raise RetrievalStoreError("LanceDB writer lock path is unavailable")
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import portalocker

            with portalocker.Lock(
                self._lock_path,
                mode="a",
                timeout=self._lock_timeout_seconds,
                flags=(
                    portalocker.LockFlags.EXCLUSIVE
                    | portalocker.LockFlags.NON_BLOCKING
                ),
            ):
                yield
        except RetrievalStoreError:
            raise
        except Exception as exc:
            raise RetrievalStoreError("LanceDB writer lock is unavailable") from exc

    def _resolve_table(self) -> object | None:
        if self._resolved:
            return self._table
        self._resolved = True
        if not self._uri:
            return None
        try:
            import lancedb

            database = lancedb.connect(self._uri)
            self._table = database.open_table(self._table_name)
        except Exception:
            self._table = None
        return self._table

    def _refresh_reader(self) -> object:
        table = self._resolve_table()
        if table is None:
            raise RetrievalStoreError("LanceDB retrieval table is unavailable")
        try:
            checkout_latest = getattr(table, "checkout_latest", None)
            if callable(checkout_latest):
                checkout_latest()
        except Exception as exc:
            raise RetrievalStoreError("LanceDB reader refresh failed") from exc
        return table

    def refresh(self) -> None:
        """Refresh a reader checkout after a committed writer update.

        Readers never take the writer lock: LanceDB's checkout is refreshed
        before each observation, while all mutations/maintenance remain
        serialized by ``_exclusive_lock``.
        """
        self._refresh_reader()

    @contextmanager
    def maintenance_lock(self) -> Iterator[None]:
        """Expose the same exclusive lock for reviewed maintenance commands."""
        with self._exclusive_lock():
            yield

    @staticmethod
    def _rows(query: object) -> list[Mapping[str, object]]:
        try:
            values = query.to_list()  # type: ignore[attr-defined]
        except Exception as exc:
            raise RetrievalStoreError("LanceDB query failed") from exc
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, Mapping)]

    @staticmethod
    def _version(value: str | int) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("document version must be a string or integer")
        normalized = str(value)
        if not 1 <= len(normalized) <= 160:
            raise ValueError("document version is invalid")
        return normalized

    def ensure_schema(self, schema_version: str | int) -> None:
        if schema_version in ("", None):
            raise ValueError("LanceDB schema version is required")
        table = self._refresh_reader()
        names = set(getattr(getattr(table, "schema", None), "names", []))
        if names and not self._REQUIRED_COLUMNS.issubset(names):
            raise RetrievalStoreError("LanceDB retrieval schema is incompatible")

    def upsert_chunks(
        self,
        document_version: str | int,
        chunks: Sequence[RetrievalChunk],
        embedding_metadata: Mapping[str, object] | None = None,
    ) -> None:
        version = self._version(document_version)
        rows: list[dict[str, object]] = []
        metadata = embedding_metadata or {}
        vectors = metadata.get("dense_vectors", {})
        for chunk in chunks:
            if (
                not chunk.chunk_id
                or len(chunk.chunk_id) > 160
                or type(chunk.document_id) is not int
                or chunk.document_id <= 0
                or type(chunk.chunk_no) is not int
                or chunk.chunk_no < 0
            ):
                raise ValueError("LanceDB chunk identity is invalid")
            row = {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "version_id": version,
                "chunk_no": chunk.chunk_no,
                "text": chunk.text,
                "metadata_json": json.dumps(
                    dict(chunk.metadata),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
            if isinstance(vectors, Mapping) and chunk.chunk_id in vectors:
                row["vector"] = list(vectors[chunk.chunk_id])
            rows.append(row)
        if not rows:
            return
        with self._exclusive_lock():
            table = self._refresh_reader()
            try:
                if hasattr(table, "merge_insert"):
                    (
                        table.merge_insert("chunk_id")
                        .when_matched_update_all()
                        .when_not_matched_insert_all()
                        .execute(rows)
                    )
                elif hasattr(table, "add"):
                    table.add(rows)
                else:
                    raise RetrievalStoreError("LanceDB table is not writable")
            except RetrievalStoreError:
                raise
            except Exception as exc:
                raise RetrievalStoreError("LanceDB chunk upsert failed") from exc

    def delete_document(self, document_id: int) -> None:
        if type(document_id) is not int or document_id <= 0:
            raise ValueError("document_id must be a positive integer")
        with self._exclusive_lock():
            table = self._refresh_reader()
            try:
                table.delete(f"document_id = {document_id}")  # type: ignore[attr-defined]
            except Exception as exc:
                raise RetrievalStoreError("LanceDB document delete failed") from exc

    def search(
        self,
        query: str,
        authorized_filter: AuthorizedFilter | None,
        limit: int,
    ) -> list[RetrievalHit]:
        if not query or limit <= 0:
            return []
        allowed = (
            authorized_filter.document_ids
            if authorized_filter is not None
            else None
        )
        if allowed is not None and not allowed:
            return []
        table = self._refresh_reader()
        try:
            builder = table.search(  # type: ignore[attr-defined]
                query,
                query_type="fts",
                fts_columns="text",
            )
            if allowed is not None:
                values = ",".join(str(value) for value in sorted(allowed))
                builder = builder.where(
                    f"document_id IN ({values})",
                    prefilter=True,
                )
            builder = builder.limit(max(limit * 3, limit))
            rows = self._rows(builder)
        except RetrievalStoreError:
            raise
        except Exception as exc:
            raise RetrievalStoreError("LanceDB search failed") from exc

        best: dict[int, RetrievalHit] = {}
        for row in rows:
            document_id = row.get("document_id")
            if type(document_id) is not int or document_id <= 0:
                continue
            if allowed is not None and document_id not in allowed:
                continue
            chunk_id = row.get("chunk_id")
            version_id = row.get("version_id")
            text_value = row.get("text")
            score_value = row.get("_score", row.get("score", 0.0))
            try:
                score = float(score_value)
            except (TypeError, ValueError):
                score = 0.0
            hit = RetrievalHit(
                document_id=document_id,
                snippet=str(text_value or "")[:400],
                score=score,
                source=self.adapter_name,
                chunk_id=str(chunk_id) if chunk_id is not None else None,
                version_id=(
                    version_id
                    if isinstance(version_id, (str, int)) and not isinstance(version_id, bool)
                    else None
                ),
            )
            current = best.get(document_id)
            if current is None or (hit.score, hit.chunk_id or "") > (
                current.score,
                current.chunk_id or "",
            ):
                best[document_id] = hit
        return sorted(
            best.values(),
            key=lambda item: (-item.score, item.document_id, item.chunk_id or ""),
        )[:limit]

    def document_index_state(
        self, document_id: int, version_id: str | int | None = None
    ) -> DocumentIndexState:
        if type(document_id) is not int or document_id <= 0:
            raise ValueError("document_id must be a positive integer")
        table = self._refresh_reader()
        try:
            builder = (
                table.search()  # type: ignore[attr-defined]
                .where(f"document_id = {document_id}", prefilter=True)
                .select(["version_id"])
                .limit(100)
            )
            rows = self._rows(builder)
        except RetrievalStoreError:
            raise
        except Exception as exc:
            raise RetrievalStoreError("LanceDB index-state query failed") from exc
        versions = {
            value
            for row in rows
            for value in [row.get("version_id")]
            if isinstance(value, (str, int)) and not isinstance(value, bool)
        }
        indexed = bool(versions)
        indexed_version = next(iter(versions)) if len(versions) == 1 else None
        requested = str(version_id) if version_id is not None else None
        return DocumentIndexState(
            document_id,
            indexed,
            version_id,
            indexed_version,
            (
                indexed and {str(value) for value in versions} == {requested}
                if requested is not None
                else None
            ),
        )

    def health(self) -> RetrievalHealth:
        try:
            self.ensure_schema("current")
        except Exception:
            return RetrievalHealth(self.adapter_name, False, "schema_or_refresh_failed")
        return RetrievalHealth(self.adapter_name, True, "ready")

    def optimize(self) -> RetrievalMaintenance:
        with self._exclusive_lock():
            table = self._refresh_reader()
            try:
                table.optimize()  # type: ignore[attr-defined]
            except Exception as exc:
                raise RetrievalStoreError("LanceDB maintenance failed") from exc
        return RetrievalMaintenance(self.adapter_name, True, "optimized_under_lock")

    def reconcile(
        self,
        authoritative_versions: Mapping[int, str | int | None],
        *,
        max_items: int = 10_000,
    ) -> RetrievalReconciliation:
        if type(max_items) is not int or not 1 <= max_items <= 100_000:
            raise ValueError("max_items must be between 1 and 100000")
        table = self._refresh_reader()
        try:
            rows = self._rows(
                table.search()  # type: ignore[attr-defined]
                .select(["document_id", "version_id"])
                .limit(max_items)
            )
        except RetrievalStoreError:
            raise
        except Exception as exc:
            raise RetrievalStoreError("LanceDB reconciliation failed") from exc
        indexed_versions: dict[int, set[str]] = defaultdict(set)
        for row in rows:
            document_id = row.get("document_id")
            version_id = row.get("version_id")
            if (
                type(document_id) is int
                and document_id > 0
                and isinstance(version_id, (str, int))
                and not isinstance(version_id, bool)
            ):
                indexed_versions[document_id].add(str(version_id))
        indexed = set(indexed_versions)
        expected = set(authoritative_versions)
        lag = {
            document_id
            for document_id in indexed & expected
            if authoritative_versions[document_id] is not None
            and indexed_versions[document_id]
            != {str(authoritative_versions[document_id])}
        }
        return RetrievalReconciliation(
            adapter=self.adapter_name,
            checked=True,
            indexed_document_ids=frozenset(sorted(indexed)[:max_items]),
            missing_document_ids=frozenset(sorted(expected - indexed)[:max_items]),
            stale_document_ids=frozenset(sorted(indexed - expected)[:max_items]),
            version_lag_document_ids=frozenset(sorted(lag)[:max_items]),
            detail="reader_refreshed_before_reconciliation",
        )
