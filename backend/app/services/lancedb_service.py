"""Configured LanceDB reader, single-writer, and maintenance factories."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence

from ..config import settings
from ..retrieval_store import (
    LanceDbRetrievalStore,
    RetrievalMaintenance,
    RetrievalStoreError,
)

_reader: LanceDbRetrievalStore | None = None
_reader_key: tuple[str, str] | None = None
_reader_lock = threading.Lock()

_SEMANTIC_TABLES = {
    "page": "visual_semantic_pages_v1",
    "image": "visual_semantic_images_v1",
}


def visual_index_schema(*, dimension: int) -> dict[str, object]:
    """Return the reviewed scalar/vector contract for visual Lance tables."""
    if not 8 <= dimension <= 4096:
        raise ValueError("visual index dimension is invalid")
    return {
        "visual_pages": ("asset_id", "document_id", "version_id", "page_number", "vectors", "metadata_json"),
        "image_assets": ("asset_id", "document_id", "version_id", "page_number", "vector", "metadata_json"),
        "dimension": dimension,
    }


def provision_visual_indexes(*, dimension: int, uri: str | None = None) -> dict[str, object]:
    """Explicitly provision visual tables; never runs during API startup."""
    if not settings.lancedb_writer_enabled:
        raise RetrievalStoreError("LanceDB writer role is disabled")
    contract = visual_index_schema(dimension=dimension)
    target = uri or str(settings.lancedb_uri)
    try:
        import lancedb
        import pyarrow as pa

        database = lancedb.connect(target)
        for name in ("visual_pages", "image_assets"):
            vector_field = "vectors" if name == "visual_pages" else "vector"
            schema = pa.schema([
                pa.field("asset_id", pa.int64(), nullable=False),
                pa.field("document_id", pa.int64(), nullable=False),
                pa.field("version_id", pa.string(), nullable=False),
                pa.field("page_number", pa.int32(), nullable=True),
                pa.field(vector_field, pa.list_(pa.float32(), dimension), nullable=False),
                pa.field("metadata_json", pa.string(), nullable=False),
            ])
            table = database.create_table(name, schema=schema, exist_ok=True)
            table.create_scalar_index("document_id", replace=True)
            table.create_scalar_index("version_id", replace=True)
        return contract
    except Exception as exc:
        raise RetrievalStoreError("visual LanceDB schema provisioning failed") from exc


def visual_semantic_table_name(lane: str) -> str:
    if lane not in _SEMANTIC_TABLES:
        raise ValueError("semantic visual lane is invalid")
    return _SEMANTIC_TABLES[lane]


def provision_visual_semantic_indexes(*, dimension: int, uri: str | None = None) -> dict[str, object]:
    """Create model-versioned semantic image/page tables under the writer lock."""

    if not settings.lancedb_writer_enabled:
        raise RetrievalStoreError("LanceDB writer role is disabled")
    if not 8 <= dimension <= 4096:
        raise ValueError("semantic visual index dimension is invalid")
    target = uri or str(settings.lancedb_uri)
    try:
        import lancedb
        import pyarrow as pa

        store = writer_store()
        with store.maintenance_lock():
            database = lancedb.connect(target)
            for name in _SEMANTIC_TABLES.values():
                schema = pa.schema([
                    pa.field("embedding_key", pa.string(), nullable=False),
                    pa.field("asset_id", pa.int64(), nullable=False),
                    pa.field("document_id", pa.int64(), nullable=False),
                    pa.field("version_id", pa.string(), nullable=False),
                    pa.field("page_number", pa.int32(), nullable=True),
                    pa.field("vector", pa.list_(pa.float32(), dimension), nullable=False),
                    pa.field("model_revision", pa.string(), nullable=False),
                    pa.field("checksum", pa.string(), nullable=False),
                    pa.field("lifecycle_state", pa.string(), nullable=False),
                    pa.field("metadata_json", pa.string(), nullable=False),
                ])
                table = database.create_table(name, schema=schema, exist_ok=True)
                table.create_scalar_index("document_id", replace=True)
                table.create_scalar_index("model_revision", replace=True)
                table.create_scalar_index("lifecycle_state", replace=True)
        return {
            "dimension": dimension,
            "tables": dict(_SEMANTIC_TABLES),
            "metric": "dot",
            "storage": "float32",
        }
    except RetrievalStoreError:
        raise
    except Exception as exc:
        raise RetrievalStoreError("semantic visual LanceDB schema provisioning failed") from exc


def visual_semantic_indexes_available(*, dimension: int, uri: str | None = None) -> bool:
    """Check both semantic tables without acquiring the writer lock or mutating."""

    if not 8 <= dimension <= 4096:
        return False
    try:
        import lancedb

        database = lancedb.connect(uri or str(settings.lancedb_uri))
        for name in _SEMANTIC_TABLES.values():
            table = database.open_table(name)
            schema = getattr(table, "schema", None)
            names = set(getattr(schema, "names", []))
            if not {
                "embedding_key",
                "asset_id",
                "document_id",
                "vector",
                "model_revision",
                "lifecycle_state",
            }.issubset(names):
                return False
            vector_field = schema.field("vector") if schema is not None else None
            vector_type = getattr(vector_field, "type", None)
            if getattr(vector_type, "list_size", None) != dimension:
                return False
        return True
    except Exception:
        return False


def upsert_visual_semantic_rows(
    lane: str,
    rows: Sequence[Mapping[str, object]],
    *,
    uri: str | None = None,
    dimension: int | None = None,
) -> int:
    """Upsert bounded semantic rows through the designated LanceDB writer."""

    if not rows:
        return 0
    if not settings.lancedb_writer_enabled:
        raise RetrievalStoreError("LanceDB writer role is disabled")
    table_name = visual_semantic_table_name(lane)
    target = uri or str(settings.lancedb_uri)
    vector_dimension = dimension or settings.visual_semantic_dimension
    if not 8 <= vector_dimension <= 4096:
        raise ValueError("semantic embedding dimension is invalid")
    cleaned: list[dict[str, object]] = []
    for raw in rows:
        row = dict(raw)
        key = row.get("embedding_key")
        asset_id = row.get("asset_id")
        document_id = row.get("document_id")
        vector = row.get("vector")
        if not isinstance(key, str) or not 1 <= len(key) <= 300:
            raise ValueError("semantic embedding key is invalid")
        if type(asset_id) is not int or asset_id <= 0 or type(document_id) is not int or document_id <= 0:
            raise ValueError("semantic embedding identity is invalid")
        if not isinstance(vector, (list, tuple)) or len(vector) != vector_dimension:
            raise ValueError("semantic embedding dimension is invalid")
        cleaned.append(row)
    try:
        import lancedb

        store = writer_store()
        with store.maintenance_lock():
            table = lancedb.connect(target).open_table(table_name)
            if hasattr(table, "merge_insert"):
                (
                    table.merge_insert("embedding_key")
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(cleaned)
                )
            else:
                table.add(cleaned)
        return len(cleaned)
    except RetrievalStoreError:
        raise
    except Exception as exc:
        raise RetrievalStoreError("semantic visual LanceDB upsert failed") from exc


def search_visual_semantic(
    lane: str,
    vector: Sequence[float],
    *,
    authorized_ids: frozenset[int],
    model_revision: str,
    limit: int,
) -> list[dict[str, object]]:
    """Search a semantic lane with an exact document-ID prefilter."""

    if lane not in _SEMANTIC_TABLES or not vector or not authorized_ids or not model_revision:
        return []
    if not 1 <= limit <= 200:
        return []
    if len(authorized_ids) > settings.visual_semantic_prefilter_max_ids:
        # The caller must use a materialized security-domain projection before
        # sending very large authorization sets to an ANN index.  Returning no
        # semantic rows is safer than searching an unfiltered table.
        return []
    values = ",".join(str(value) for value in sorted(authorized_ids) if type(value) is int and value > 0)
    if not values:
        return []
    safe_revision = model_revision.replace("'", "''")
    try:
        import lancedb

        table = lancedb.connect(str(settings.lancedb_uri)).open_table(visual_semantic_table_name(lane))
        builder = table.search(list(vector))
        builder = builder.distance_type("dot")
        builder = builder.where(
            "lifecycle_state = 'ACTIVE' "
            f"AND model_revision = '{safe_revision}' "
            f"AND document_id IN ({values})",
            prefilter=True,
        )
        rows = builder.limit(min(limit, settings.visual_semantic_max_query_assets)).to_list()
    except Exception:
        return []
    results: list[dict[str, object]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        asset_id = row.get("asset_id")
        document_id = row.get("document_id")
        if type(asset_id) is not int or type(document_id) is not int or document_id not in authorized_ids:
            continue
        raw_distance = row.get("_distance", row.get("_score", 0.0))
        try:
            # LanceDB's dot distance is 1 - dot for normalized vectors.  Keep
            # the public score in [0, 1] while retaining the raw value.
            distance = float(raw_distance)
        except (TypeError, ValueError):
            continue
        score = max(0.0, min(1.0, 1.0 - distance))
        metadata_value = row.get("metadata_json")
        try:
            metadata = json.loads(str(metadata_value or "{}"))
        except (TypeError, ValueError):
            metadata = {}
        results.append({
            "asset_id": asset_id,
            "document_id": document_id,
            "version_id": row.get("version_id"),
            "page_number": row.get("page_number"),
            "score": score,
            "raw_distance": distance,
            "metadata": metadata if isinstance(metadata, dict) else {},
            "result_type": "page" if lane == "page" else "image",
        })
    results.sort(key=lambda item: (-float(item["score"]), int(item["asset_id"])))
    return results[:limit]


def reader_store() -> LanceDbRetrievalStore:
    """Return the process reader; reads refresh the checkout before observing."""
    global _reader, _reader_key
    key = (str(settings.lancedb_uri), settings.lancedb_table_name)
    with _reader_lock:
        if _reader is None or _reader_key != key:
            _reader = LanceDbRetrievalStore(
                settings.lancedb_uri,
                table_name=settings.lancedb_table_name,
                writer=False,
                lock_timeout_seconds=settings.lancedb_lock_timeout_seconds,
            )
            _reader_key = key
        return _reader


def writer_store() -> LanceDbRetrievalStore:
    """Create a mutation-capable adapter only in the designated writer process."""
    if not settings.lancedb_writer_enabled:
        raise RetrievalStoreError("LanceDB writer role is disabled")
    return LanceDbRetrievalStore(
        settings.lancedb_uri,
        table_name=settings.lancedb_table_name,
        writer=True,
        lock_timeout_seconds=settings.lancedb_lock_timeout_seconds,
    )


def provision() -> LanceDbRetrievalStore:
    if not settings.lancedb_writer_enabled:
        raise RetrievalStoreError("LanceDB writer role is disabled")
    return LanceDbRetrievalStore.provision(
        settings.lancedb_uri,
        table_name=settings.lancedb_table_name,
        vector_dimensions=(
            settings.qdrant_vector_size
            if (settings.enable_embeddings or settings.lancedb_text_vectors_enabled)
            else None
        ),
        lock_timeout_seconds=settings.lancedb_lock_timeout_seconds,
    )


def optimize() -> RetrievalMaintenance:
    store = writer_store()
    store.ensure_schema("current")
    return store.optimize()


def search_image_exact(data: bytes, *, authorized_ids: frozenset[int], limit: int = 20) -> list[dict[str, object]]:
    """Return exact content-addressed visual matches from LanceDB.

    This is the safe first visual lane: it never invents similarity scores and
    only returns rows whose content hash matches the ephemeral query and whose
    document is in the caller's SQL-authorized set.
    """
    if not data or not authorized_ids or not 1 <= limit <= 100:
        return []
    try:
        import lancedb

        table = lancedb.connect(str(settings.lancedb_uri)).open_table("image_assets")
        target = hashlib.sha256(data).hexdigest()
        rows = table.to_arrow().to_pylist()
    except Exception:
        return []
    matches: list[dict[str, object]] = []
    for row in rows:
        document_id = row.get("document_id")
        if type(document_id) is not int or document_id not in authorized_ids:
            continue
        try:
            metadata = json.loads(str(row.get("metadata_json") or "{}"))
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("checksum") != target:
            continue
        matches.append({
            "document_id": document_id,
            "asset_id": row.get("asset_id"),
            "version_id": row.get("version_id"),
            "page_number": row.get("page_number"),
            "score": 1.0,
            "source": "lancedb",
            "result_type": "image",
        })
        if len(matches) >= limit:
            break
    return matches


__all__ = [
    "optimize",
    "provision",
    "provision_visual_indexes",
    "provision_visual_semantic_indexes",
    "reader_store",
    "search_image_exact",
    "search_visual_semantic",
    "upsert_visual_semantic_rows",
    "visual_index_schema",
    "visual_semantic_indexes_available",
    "visual_semantic_table_name",
    "writer_store",
]
