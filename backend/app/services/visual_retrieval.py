"""Typed, authorization-first visual retrieval primitives.

The module is provider-neutral: visual model/index adapters supply bounded lane
callbacks, while routing, fusion, reranking, and security trimming stay here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


class VisualQueryMode(StrEnum):
    TEXT_TO_PAGE = "text_to_page"
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"
    HYBRID = "hybrid"


class VisualLane(StrEnum):
    PAGE = "page"
    IMAGE = "image"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class LaneBudget:
    page: int = 20
    image: int = 20
    text: int = 20

    def for_lane(self, lane: VisualLane) -> int:
        value = getattr(self, lane.value)
        if type(value) is not int or not 1 <= value <= 200:
            raise ValueError("visual lane budget must be between 1 and 200")
        return value


@dataclass(frozen=True, slots=True)
class AuthorizedIdProjection:
    """Versioned large-ID projection; stale/mismatched projections fail closed."""

    user_id: int
    policy_revision: int
    document_ids: frozenset[int]

    def ids_for_lane(self, *, user_id: int, policy_revision: int) -> frozenset[int]:
        if user_id != self.user_id or policy_revision != self.policy_revision:
            return frozenset()
        return self.document_ids


def build_authorized_id_projection(
    user_id: int,
    policy_revision: int,
    document_ids: Iterable[int],
) -> AuthorizedIdProjection:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("user_id must be positive")
    if type(policy_revision) is not int or policy_revision < 0:
        raise ValueError("policy_revision must be non-negative")
    return AuthorizedIdProjection(
        user_id=user_id,
        policy_revision=policy_revision,
        document_ids=frozenset(value for value in document_ids if type(value) is int and value > 0),
    )


def _candidate_document_id(candidate: dict) -> int | None:
    value = candidate.get("document_id")
    return value if type(value) is int and value > 0 else None


def exact_prefilter(candidates: Iterable[dict], allowed_ids: frozenset[int]) -> list[dict]:
    """Apply the same exact SQL-authorized document set to every visual lane."""
    return [candidate for candidate in candidates if _candidate_document_id(candidate) in allowed_ids]


def _dedupe_key(candidate: dict) -> tuple:
    asset_id = candidate.get("asset_id")
    if type(asset_id) is int and asset_id > 0:
        return ("asset", asset_id)
    return (
        "lineage",
        candidate.get("document_id"),
        candidate.get("version_id"),
        candidate.get("page_number"),
        candidate.get("result_type"),
    )


def reciprocal_rank_fusion(
    lane_results: dict[VisualLane, list[dict]],
    *,
    limit: int,
    allowed_ids: frozenset[int],
    rank_constant: int = 60,
) -> list[dict]:
    """Fuse ranks only; incompatible provider scores are never added together."""
    if not 1 <= limit <= 100 or rank_constant < 1:
        raise ValueError("invalid fusion bounds")
    merged: dict[tuple, dict] = {}
    lane_names: defaultdict[tuple, set[str]] = defaultdict(set)
    rank_scores: defaultdict[tuple, float] = defaultdict(float)
    for lane, candidates in lane_results.items():
        for rank, candidate in enumerate(exact_prefilter(candidates, allowed_ids), start=1):
            key = _dedupe_key(candidate)
            lane_names[key].add(lane.value)
            rank_scores[key] += 1.0 / (rank_constant + rank)
            merged.setdefault(key, dict(candidate))
    output: list[dict] = []
    for key, candidate in merged.items():
        candidate["fusion_score"] = round(rank_scores[key], 8)
        candidate["matched_lanes"] = sorted(lane_names[key])
        output.append(candidate)
    output.sort(key=lambda item: (-item["fusion_score"], _dedupe_key(item)))
    return output[:limit]


def apply_optional_local_reranker(
    query: str,
    candidates: list[dict],
    *,
    enabled: bool,
    reranker: Callable[[str, str], float] | None = None,
) -> list[dict]:
    """Apply a local callback only when explicitly enabled; failures preserve order."""
    if not enabled or reranker is None or not candidates:
        return candidates
    try:
        scored = []
        for candidate in candidates:
            text = str(candidate.get("snippet") or candidate.get("text") or "")[:4000]
            scored.append((float(reranker(query, text)), candidate))
        scored.sort(key=lambda item: (-item[0], _dedupe_key(item[1])))
        for score, candidate in scored:
            candidate["rerank_score"] = round(score, 8)
        return [candidate for _, candidate in scored]
    except Exception:
        return candidates


LaneSearcher = Callable[[str, frozenset[int], int], list[dict]]


def route_typed_query(
    *,
    mode: VisualQueryMode | str,
    query: str,
    allowed_ids: frozenset[int],
    limit: int,
    lane_searchers: dict[VisualLane, LaneSearcher],
    budgets: LaneBudget = LaneBudget(),
    reranker_enabled: bool = False,
    reranker: Callable[[str, str], float] | None = None,
) -> list[dict]:
    """Route only supported lanes, with bounded candidates and exact prefilters."""
    selected = VisualQueryMode(mode)
    if not query.strip() or not allowed_ids:
        return []
    lanes = {
        VisualQueryMode.TEXT_TO_PAGE: (VisualLane.PAGE,),
        VisualQueryMode.TEXT_TO_IMAGE: (VisualLane.IMAGE,),
        VisualQueryMode.IMAGE_TO_IMAGE: (VisualLane.IMAGE,),
        VisualQueryMode.HYBRID: (VisualLane.TEXT, VisualLane.PAGE, VisualLane.IMAGE),
    }[selected]
    results: dict[VisualLane, list[dict]] = {}
    for lane in lanes:
        searcher = lane_searchers.get(lane)
        if searcher is None:
            continue
        try:
            results[lane] = exact_prefilter(searcher(query, allowed_ids, budgets.for_lane(lane)), allowed_ids)
        except Exception:
            results[lane] = []
    fused = reciprocal_rank_fusion(results, limit=limit, allowed_ids=allowed_ids)
    return apply_optional_local_reranker(query, fused, enabled=reranker_enabled, reranker=reranker)[:limit]


def persist_visual_output(
    db: Session,
    *,
    asset_id: int,
    version_id: int,
    output_type: str,
    text: str,
    engine_revision: str,
    confidence: float | None = None,
    language: str | None = None,
    quality_signals: dict[str, object] | None = None,
) -> models.VisualExtraction:
    """Persist OCR/caption output as untrusted, versioned provenance."""
    if output_type not in {"OCR", "CAPTION", "DESCRIPTION"}:
        raise ValueError("visual output type is invalid")
    if not engine_revision or len(engine_revision) > 160:
        raise ValueError("visual engine revision is invalid")
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise ValueError("visual confidence must be between zero and one")
    existing = db.scalar(select(models.VisualExtraction).where(
        models.VisualExtraction.asset_id == asset_id,
        models.VisualExtraction.output_type == output_type,
        models.VisualExtraction.engine_revision == engine_revision,
    ))
    if existing is not None:
        return existing
    output = models.VisualExtraction(
        asset_id=asset_id,
        version_id=version_id,
        output_type=output_type,
        text=(text or "")[:5_000_000],
        engine_revision=engine_revision,
        confidence=confidence,
        language=language[:40] if language else None,
        quality_signals=json.dumps(quality_signals or {}, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        trusted=False,
    )
    db.add(output)
    db.flush()
    return output


__all__ = [
    "AuthorizedIdProjection", "LaneBudget", "VisualLane", "VisualQueryMode",
    "apply_optional_local_reranker", "build_authorized_id_projection",
    "exact_prefilter", "persist_visual_output", "reciprocal_rank_fusion", "route_typed_query",
]
