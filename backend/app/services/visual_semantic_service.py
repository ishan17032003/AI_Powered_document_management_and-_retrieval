"""Model-backed semantic visual indexing and query primitives.

The service owns model lifecycle and LanceDB writes, while
``visual_search_service`` remains responsible for typed routing, rank fusion,
and final SQL reauthorization.  Any unavailable model/index returns a bounded
degraded result so the lexical visual lane can continue serving.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..storage import object_store
from . import lancedb_service
from .visual_embeddings import VisualEmbedding
from .visual_semantic_embeddings import (
    Siglip2EmbeddingAdapter,
    VisualModelUnavailable,
    artifact_sha256,
)

SEMANTIC_IMAGE_MANIFEST_LANE = "semantic_image"
SEMANTIC_PAGE_MANIFEST_LANE = "semantic_page"
PROMPT_TEMPLATE_VERSION = "siglip2-photo-v1"


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    available: bool
    lane_results: dict[str, list[dict[str, object]]]
    provider: str = "siglip2"
    degraded: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticIndexResult:
    state: str
    image_count: int = 0
    page_count: int = 0
    error_code: str | None = None


_adapter: Siglip2EmbeddingAdapter | None = None
_adapter_key: tuple[object, ...] | None = None
_test_adapter: Siglip2EmbeddingAdapter | None = None


def set_adapter_for_tests(adapter: Siglip2EmbeddingAdapter | None) -> None:
    """Inject a tiny fake-backed adapter without importing torch in tests."""

    global _test_adapter
    _test_adapter = adapter


def reset_adapter() -> None:
    global _adapter, _adapter_key, _test_adapter
    _adapter = None
    _adapter_key = None
    _test_adapter = None


def configured_adapter() -> Siglip2EmbeddingAdapter:
    """Return the cached adapter or fail closed with a stable unavailable error."""

    if _test_adapter is not None:
        return _test_adapter
    if not settings.visual_semantic_search_enabled and not settings.visual_semantic_ingestion_enabled:
        raise VisualModelUnavailable("semantic visual search is disabled")
    model_path = settings.visual_semantic_model_path or (
        settings.storage_dir / "docvault-siglip2"
    )
    model_sha256 = settings.visual_semantic_model_sha256.strip()
    if not model_sha256:
        try:
            model_sha256 = artifact_sha256(Path(model_path))
        except VisualModelUnavailable as exc:
            raise VisualModelUnavailable("semantic model artifact is not available") from exc
    key = (
        str(model_path),
        settings.visual_semantic_model_revision,
        model_sha256.lower(),
        settings.visual_semantic_dimension,
        settings.visual_semantic_device,
        settings.visual_semantic_max_batch,
    )
    global _adapter, _adapter_key
    if _adapter is None or _adapter_key != key:
        _adapter = Siglip2EmbeddingAdapter(
            model_path=Path(model_path),
            model_revision=settings.visual_semantic_model_revision,
            model_sha256=model_sha256,
            dimension=settings.visual_semantic_dimension,
            device=settings.visual_semantic_device,
            max_batch=settings.visual_semantic_max_batch,
        )
        _adapter_key = key
    return _adapter


def _prompt(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise ValueError("semantic visual query is empty")
    # The SigLIP documentation uses a photo prompt for image/text similarity.
    # Keep the user text intact so Hindi/mixed-language queries are not silently
    # translated or sent to an external service.
    return f"a photo of {normalized}"


def _manifest(db: Session, lane: str, adapter: Siglip2EmbeddingAdapter) -> models.VisualRetrievalManifest:
    result = db.scalar(
        select(models.VisualRetrievalManifest).where(
            models.VisualRetrievalManifest.lane == lane,
            models.VisualRetrievalManifest.manifest_version == settings.visual_semantic_manifest_version,
        )
    )
    if result is None:
        result = models.VisualRetrievalManifest(
            lane=lane,
            manifest_version=settings.visual_semantic_manifest_version,
            model_revision=adapter.model_revision,
            model_sha256=adapter.model_sha256,
            vector_dimension=adapter.dimension,
            state="BUILDING",
        )
        db.add(result)
        db.flush()
    else:
        result.model_revision = adapter.model_revision
        result.model_sha256 = adapter.model_sha256
        result.vector_dimension = adapter.dimension
        result.state = "BUILDING"
        result.retired_at = None
        db.flush()
    return result


def _asset_payload(asset: models.VisualAsset) -> bytes:
    with object_store.open(asset.file_key) as handle:
        payload = handle.read(settings.max_upload_bytes + 1)
    if not isinstance(payload, bytes) or not payload or len(payload) > settings.max_upload_bytes:
        raise VisualModelUnavailable("visual asset exceeds semantic embedding budget")
    return payload


def _semantic_assets(db: Session, version_id: int) -> list[models.VisualAsset]:
    return list(
        db.scalars(
            select(models.VisualAsset)
            .where(
                models.VisualAsset.version_id == version_id,
                models.VisualAsset.lifecycle_state == "ACTIVE",
                models.VisualAsset.asset_type.in_(("PAGE", "IMAGE", "REGION")),
            )
            .order_by(models.VisualAsset.id)
            .limit(settings.visual_max_assets_per_version)
        ).all()
    )


def _rows_for_assets(
    assets: Sequence[models.VisualAsset],
    embeddings: Sequence[VisualEmbedding],
    *,
    title: str,
    adapter: Siglip2EmbeddingAdapter,
) -> dict[str, list[dict[str, object]]]:
    if len(assets) != len(embeddings):
        raise VisualModelUnavailable("semantic asset/vector count mismatch")
    rows: dict[str, list[dict[str, object]]] = {"image": [], "page": []}
    for asset, embedding in zip(assets, embeddings, strict=True):
        lane = "page" if asset.asset_type == "PAGE" else "image"
        rows[lane].append(
            {
                "embedding_key": f"{asset.id}:{adapter.model_revision}:{asset.checksum}",
                "asset_id": asset.id,
                "document_id": asset.document_id,
                "version_id": str(asset.version_id),
                "page_number": asset.page_number,
                "vector": list(embedding.vector),
                "model_revision": adapter.model_revision,
                "checksum": asset.checksum,
                "lifecycle_state": asset.lifecycle_state,
                "metadata_json": json.dumps(
                    {
                        "title": title,
                        "asset_type": asset.asset_type,
                        "content_type": asset.content_type,
                        "prompt_template": PROMPT_TEMPLATE_VERSION,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
        )
    return rows


def index_version(
    db: Session,
    *,
    document: models.Document,
    version: models.DocVersion,
) -> SemanticIndexResult:
    """Embed active visual assets for one version and upsert them idempotently."""

    if not settings.visual_semantic_ingestion_enabled:
        return SemanticIndexResult("disabled")
    if not settings.lancedb_writer_enabled:
        return SemanticIndexResult("degraded", error_code="LANCEDB_WRITER_DISABLED")
    try:
        adapter = configured_adapter()
        assets = _semantic_assets(db, version.id)
        if not assets:
            return SemanticIndexResult("ready")
        payloads = [_asset_payload(asset) for asset in assets]
        embeddings = adapter.embed_images(payloads, modality="image")
        rows = _rows_for_assets(assets, embeddings, title=document.title, adapter=adapter)
        if not lancedb_service.visual_semantic_indexes_available(dimension=adapter.dimension):
            lancedb_service.provision_visual_semantic_indexes(dimension=adapter.dimension)
        counts: dict[str, int] = {}
        for lane, lane_rows in rows.items():
            if not lane_rows:
                continue
            lancedb_service.upsert_visual_semantic_rows(
                lane,
                lane_rows,
                dimension=adapter.dimension,
            )
            manifest_lane = (
                SEMANTIC_PAGE_MANIFEST_LANE
                if lane == "page"
                else SEMANTIC_IMAGE_MANIFEST_LANE
            )
            manifest = _manifest(db, manifest_lane, adapter)
            manifest.row_count += len(lane_rows)
            manifest.state = "READY"
            db.flush()
            counts[lane] = len(lane_rows)
        return SemanticIndexResult(
            "ready",
            image_count=counts.get("image", 0),
            page_count=counts.get("page", 0),
        )
    except VisualModelUnavailable:
        return SemanticIndexResult("degraded", error_code="VISUAL_MODEL_UNAVAILABLE")
    except Exception:
        return SemanticIndexResult("degraded", error_code="VISUAL_SEMANTIC_INDEX_FAILED")


def _search(
    *,
    query_embedding: VisualEmbedding,
    lanes: Sequence[str],
    authorized_ids: frozenset[int],
    limit: int,
) -> SemanticSearchResult:
    lane_results: dict[str, list[dict[str, object]]] = {}
    deadline = monotonic() + settings.visual_semantic_query_timeout_seconds
    for lane in lanes:
        if monotonic() >= deadline:
            return SemanticSearchResult(
                False,
                {},
                degraded=True,
                error_code="VISUAL_SEMANTIC_QUERY_TIMEOUT",
            )
        lane_results[lane] = lancedb_service.search_visual_semantic(
            lane,
            query_embedding.vector,
            authorized_ids=authorized_ids,
            model_revision=query_embedding.model_revision,
            limit=limit,
        )
        if monotonic() >= deadline:
            return SemanticSearchResult(
                False,
                {},
                degraded=True,
                error_code="VISUAL_SEMANTIC_QUERY_TIMEOUT",
            )
    return SemanticSearchResult(True, lane_results)


def search_text(
    query: str,
    *,
    mode: str,
    authorized_ids: frozenset[int],
    limit: int,
) -> SemanticSearchResult:
    if not settings.visual_semantic_search_enabled:
        return SemanticSearchResult(False, {}, degraded=True, error_code="VISUAL_SEMANTIC_DISABLED")
    if not authorized_ids:
        return SemanticSearchResult(True, {}, degraded=False)
    try:
        adapter = configured_adapter()
        started = monotonic()
        embedding = adapter.embed_text([_prompt(query)])[0]
        if monotonic() - started >= settings.visual_semantic_query_timeout_seconds:
            return SemanticSearchResult(
                False,
                {},
                degraded=True,
                error_code="VISUAL_SEMANTIC_QUERY_TIMEOUT",
            )
        lanes = {
            "text_to_page": ("page",),
            "text_to_image": ("image",),
            "hybrid": ("page", "image"),
        }.get(mode)
        if lanes is None:
            raise ValueError("semantic text mode is invalid")
        return _search(
            query_embedding=embedding,
            lanes=lanes,
            authorized_ids=authorized_ids,
            limit=min(limit, settings.visual_semantic_max_query_assets),
        )
    except VisualModelUnavailable:
        return SemanticSearchResult(False, {}, degraded=True, error_code="VISUAL_MODEL_UNAVAILABLE")
    except Exception:
        return SemanticSearchResult(False, {}, degraded=True, error_code="VISUAL_SEMANTIC_QUERY_FAILED")


def search_image(
    payload: bytes,
    *,
    mode: str,
    authorized_ids: frozenset[int],
    limit: int,
) -> SemanticSearchResult:
    if not settings.visual_semantic_search_enabled:
        return SemanticSearchResult(False, {}, degraded=True, error_code="VISUAL_SEMANTIC_DISABLED")
    if not payload or not authorized_ids:
        return SemanticSearchResult(True, {}, degraded=False)
    try:
        adapter = configured_adapter()
        started = monotonic()
        embedding = adapter.embed_images([payload], modality="image")[0]
        if monotonic() - started >= settings.visual_semantic_query_timeout_seconds:
            return SemanticSearchResult(
                False,
                {},
                degraded=True,
                error_code="VISUAL_SEMANTIC_QUERY_TIMEOUT",
            )
        lanes = ("image", "page") if mode == "hybrid" else ("image",)
        return _search(
            query_embedding=embedding,
            lanes=lanes,
            authorized_ids=authorized_ids,
            limit=min(limit, settings.visual_semantic_max_query_assets),
        )
    except VisualModelUnavailable:
        return SemanticSearchResult(False, {}, degraded=True, error_code="VISUAL_MODEL_UNAVAILABLE")
    except Exception:
        return SemanticSearchResult(False, {}, degraded=True, error_code="VISUAL_SEMANTIC_QUERY_FAILED")


__all__ = [
    "PROMPT_TEMPLATE_VERSION",
    "SEMANTIC_IMAGE_MANIFEST_LANE",
    "SEMANTIC_PAGE_MANIFEST_LANE",
    "SemanticIndexResult",
    "SemanticSearchResult",
    "configured_adapter",
    "index_version",
    "reset_adapter",
    "search_image",
    "search_text",
    "set_adapter_for_tests",
]
