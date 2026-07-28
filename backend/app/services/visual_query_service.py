"""Ephemeral, bounded image-query boundary.

Visual indexes are optional; this service validates and normalizes the request
without persisting it, then serves the legacy exact content-addressed lane. The
typed `/search/visual/image` surface adds the optional semantic lane and falls
back to this exact provider when the model is not staged.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import lancedb_service
from .multimodal_policy import query_image_audit_record
from .visual_assets import normalize_visual_derivative, validate_visual_bytes_isolated


@dataclass(frozen=True, slots=True)
class EphemeralImageQueryResult:
    count: int
    hits: list[dict]
    audit: dict[str, object]
    provider: str


def run_ephemeral_image_query(
    data: bytes,
    content_type: str,
    *,
    limit: int = 20,
    authorized_ids: frozenset[int] = frozenset(),
) -> EphemeralImageQueryResult:
    """Validate/decode/normalize in bounded memory and discard all image bytes."""
    if not 1 <= limit <= 100:
        raise ValueError("image-query limit out of range")
    normalized = b""
    try:
        signal = validate_visual_bytes_isolated(data, content_type)
        normalized = normalize_visual_derivative(data, content_type)
        audit = query_image_audit_record(
            normalized,
            {"media_type": "image/png", "width": signal.width, "height": signal.height, "purpose": "query"},
        )
        hits = lancedb_service.search_image_exact(
            normalized, authorized_ids=authorized_ids, limit=limit
        )
        return EphemeralImageQueryResult(
            count=len(hits), hits=hits, audit=audit,
            provider="lancedb" if hits else "lancedb_empty",
        )
    finally:
        # Explicitly release references; no query bytes enter an asset/index table.
        data = b""
        normalized = b""


__all__ = ["EphemeralImageQueryResult", "run_ephemeral_image_query"]
