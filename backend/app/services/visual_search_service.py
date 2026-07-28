"""Authorization-first text-to-visual retrieval.

The default lane searches text already attached to visual assets: document titles
plus versioned OCR/caption/description outputs.  It is useful immediately for
scanned pages, screenshots, figures, and image files, does not require model
downloads or provider egress, and reports its provider honestly.  An optional
local SigLIP2 encoder supplies semantic candidates behind the same result
contract when its artifact and capacity gates pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..repositories import visual_search_repository
from . import visual_access, visual_semantic_service
from .search_authorization import resolve_view_document_ids_for_user_id
from .visual_retrieval import LaneBudget, VisualLane, VisualQueryMode, route_typed_query

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    candidate: visual_search_repository.VisualSearchCandidate
    score: float
    snippet: str
    matched_lanes: tuple[str, ...]


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token.casefold()
        for token in _TOKEN_RE.findall(value)
        if token.casefold() not in _STOP_WORDS
    )


def _compact(value: str, *, limit: int = 360) -> str:
    return " ".join(value.split())[:limit]


def _score_candidate(
    candidate: visual_search_repository.VisualSearchCandidate,
    query: str,
    query_tokens: tuple[str, ...],
) -> _ScoredCandidate | None:
    title = candidate.title.casefold()
    body = candidate.extraction_text.casefold()
    full = f"{title}\n{body}"
    if not query_tokens:
        return None

    title_hits = sum(title.count(token) for token in query_tokens)
    body_hits = sum(body.count(token) for token in query_tokens)
    phrase = query.casefold().strip()
    phrase_hit = bool(phrase and phrase in full)
    matched = tuple(
        token
        for token in query_tokens
        if token in title or token in body
    )
    if not matched:
        return None

    # Title and exact phrase matches are intentionally stronger than OCR text;
    # the score is only used for ordering, never as an authorization decision.
    raw_score = (title_hits * 5.0) + (body_hits * 2.0) + (len(matched) * 1.5)
    if phrase_hit:
        raw_score += 4.0
    score = min(1.0, raw_score / max(8.0, len(query_tokens) * 7.0))

    if body:
        first = min(
            (body.find(token) for token in matched if body.find(token) >= 0),
            default=0,
        )
        start = max(0, first - 100)
        snippet = _compact(candidate.extraction_text[start : start + 360])
    else:
        snippet = _compact(candidate.title)
    if not snippet:
        snippet = _compact(candidate.title)

    lane = "page" if candidate.asset_type == "PAGE" else "image"
    return _ScoredCandidate(candidate, round(score, 6), snippet, (lane,))


def _asset_types(mode: str) -> frozenset[str]:
    if mode == "text_to_page":
        return frozenset({"PAGE"})
    if mode == "text_to_image":
        return frozenset({"IMAGE", "REGION"})
    if mode == "image_to_image":
        return frozenset({"IMAGE", "REGION"})
    if mode == "hybrid":
        return frozenset({"PAGE", "IMAGE", "REGION"})
    raise ValueError("unsupported visual search mode")


def _semantic_response(
    db: Session,
    user: models.User,
    *,
    query: str,
    mode: str,
    limit: int,
    allowed_ids: frozenset[int],
    semantic: visual_semantic_service.SemanticSearchResult,
) -> schemas.VisualSearchResponse | None:
    """Return semantic hits when a model-backed index is available.

    ``None`` means the caller should use the lexical lane.  An empty semantic
    response is returned when the model/index was available and no authorized
    assets matched, so an exact lexical phrase cannot accidentally override a
    meaningful semantic zero-result.
    """

    if not semantic.available:
        return None
    if not any(semantic.lane_results.values()):
        # An enabled model with no provisioned/ready table is indistinguishable
        # from an empty index at the Lance boundary.  Give the lexical lane a
        # chance so the rollout remains useful while the writer catches up.
        return None

    def lane_searcher(lane: VisualLane):
        def search_lane(
            _query: str,
            lane_allowed_ids: frozenset[int],
            budget: int,
        ) -> list[dict]:
            values = []
            for row in semantic.lane_results.get(lane.value, []):
                if row.get("document_id") not in lane_allowed_ids:
                    continue
                score = float(row.get("score", 0.0))
                if score < settings.visual_semantic_min_score:
                    continue
                values.append(dict(row))
            values.sort(key=lambda row: (-float(row.get("score", 0.0)), int(row.get("asset_id", 0))))
            return values[:budget]

        return search_lane

    routed = route_typed_query(
        mode=VisualQueryMode(mode),
        query=query,
        allowed_ids=allowed_ids,
        limit=limit,
        lane_searchers={
            VisualLane.PAGE: lane_searcher(VisualLane.PAGE),
            VisualLane.IMAGE: lane_searcher(VisualLane.IMAGE),
        },
        budgets=LaneBudget(
            page=settings.visual_lane_candidate_budget,
            image=settings.visual_lane_candidate_budget,
            text=settings.visual_lane_candidate_budget,
        ),
        reranker_enabled=False,
    )
    asset_ids = {
        int(row["asset_id"])
        for row in routed
        if type(row.get("asset_id")) is int
    }
    candidates = visual_search_repository.candidates_by_asset_ids(
        db,
        asset_ids=asset_ids,
        document_ids=allowed_ids,
        asset_types=_asset_types(mode),
    )
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    scored: list[_ScoredCandidate] = []
    for row in routed:
        asset_id = row.get("asset_id")
        candidate = by_id.get(asset_id) if type(asset_id) is int else None
        if candidate is None:
            continue
        text = _compact(candidate.extraction_text)
        if not text:
            text = _compact(candidate.title)
        lanes = row.get("matched_lanes")
        matched_lanes = (
            tuple(value for value in lanes if isinstance(value, str))
            if isinstance(lanes, list)
            else (("page",) if candidate.asset_type == "PAGE" else ("image",))
        )
        # Rank fusion controls ordering across page/image lanes.  The public
        # relevance score remains the best calibrated model similarity.
        score = max(
            float(item.get("score", 0.0))
            for item in semantic.lane_results.get(
                "page" if candidate.asset_type == "PAGE" else "image", []
            )
            if item.get("asset_id") == candidate.asset_id
        )
        scored.append(_ScoredCandidate(candidate, round(score, 6), text, matched_lanes))

    current_allowed = resolve_view_document_ids_for_user_id(db, user.id)
    authorized_assets = visual_access.reauthorize_visual_assets(
        db,
        user_id=user.id,
        asset_ids={item.candidate.asset_id for item in scored},
    )
    authorized_by_id = {asset.id: asset for asset in authorized_assets}
    hits = [
        _to_hit(item)
        for item in scored
        if item.candidate.asset_id in authorized_by_id
        and item.candidate.document_id in current_allowed
    ]
    return schemas.VisualSearchResponse(
        query=query,
        mode=mode,  # type: ignore[arg-type]
        count=len(hits),
        hits=hits,
        provider=semantic.provider,
        degraded=semantic.degraded,
    )


def _semantic_search(
    db: Session,
    user: models.User,
    *,
    query: str,
    mode: str,
    limit: int,
    allowed_ids: frozenset[int],
) -> schemas.VisualSearchResponse | None:
    if (
        settings.visual_semantic_search_enabled
        and len(allowed_ids) > settings.visual_semantic_prefilter_max_ids
    ):
        return schemas.VisualSearchResponse(
            query=query,
            mode=mode,  # type: ignore[arg-type]
            count=0,
            hits=[],
            provider="siglip2_policy_blocked",
            degraded=True,
        )
    semantic = visual_semantic_service.search_text(
        query,
        mode=mode,
        authorized_ids=allowed_ids,
        limit=max(limit, settings.visual_lane_candidate_budget),
    )
    return _semantic_response(
        db,
        user,
        query=query,
        mode=mode,
        limit=limit,
        allowed_ids=allowed_ids,
        semantic=semantic,
    )


def search_image(
    db: Session,
    user: models.User,
    *,
    payload: bytes,
    mode: str,
    limit: int,
    allowed_ids: frozenset[int] | set[int] | None,
) -> schemas.VisualSearchResponse | None:
    """Run semantic image-to-image search without persisting query bytes."""

    if not 1 <= limit <= 100:
        raise ValueError("visual search limit out of range")
    initial_allowed = frozenset(allowed_ids or ())
    if not settings.visual_search_enabled:
        return schemas.VisualSearchResponse(
            query="[uploaded image]",
            mode=mode,  # type: ignore[arg-type]
            count=0,
            hits=[],
            provider="disabled",
            degraded=True,
        )
    if (
        settings.visual_semantic_search_enabled
        and len(initial_allowed) > settings.visual_semantic_prefilter_max_ids
    ):
        return schemas.VisualSearchResponse(
            query="[uploaded image]",
            mode=mode,  # type: ignore[arg-type]
            count=0,
            hits=[],
            provider="siglip2_policy_blocked",
            degraded=True,
        )
    semantic = visual_semantic_service.search_image(
        payload,
        mode=mode,
        authorized_ids=initial_allowed,
        limit=max(limit, settings.visual_lane_candidate_budget),
    )
    semantic_response = _semantic_response(
        db,
        user,
        query="[uploaded image]",
        mode=mode,
        limit=limit,
        allowed_ids=initial_allowed,
        semantic=semantic,
    )
    if semantic_response is not None:
        return semantic_response

    # Keep the upload surface useful before the semantic model is staged.  This
    # is an exact content-addressed fallback, never a fabricated similarity
    # score; the provider is reported separately from the semantic lane.
    candidates = visual_search_repository.candidates_by_checksum(
        db,
        payload=payload,
        document_ids=initial_allowed,
        asset_types=frozenset({"IMAGE", "REGION"}),
        limit=limit,
    )
    asset_ids = {candidate.asset_id for candidate in candidates}
    current_allowed = resolve_view_document_ids_for_user_id(db, user.id)
    authorized_assets = visual_access.reauthorize_visual_assets(
        db,
        user_id=user.id,
        asset_ids=asset_ids,
    )
    authorized_asset_ids = {asset.id for asset in authorized_assets}
    hits: list[schemas.VisualSearchHit] = []
    for candidate in candidates:
        if (
            candidate.asset_id not in authorized_asset_ids
            or candidate.document_id not in current_allowed
        ):
            continue
        snippet = _compact(candidate.extraction_text) or _compact(candidate.title)
        hits.append(
            _to_hit(
                _ScoredCandidate(
                    candidate=candidate,
                    score=1.0,
                    snippet=snippet,
                    matched_lanes=("image",),
                )
            )
        )
    return schemas.VisualSearchResponse(
        query="[uploaded image]",
        mode=mode,  # type: ignore[arg-type]
        count=len(hits),
        hits=hits[:limit],
        provider="sql_exact" if hits else "sql_exact_empty",
        degraded=True,
    )


def _to_hit(item: _ScoredCandidate) -> schemas.VisualSearchHit:
    candidate = item.candidate
    return schemas.VisualSearchHit(
        asset_id=candidate.asset_id,
        document_id=candidate.document_id,
        version_id=candidate.version_id,
        title=candidate.title,
        asset_type=candidate.asset_type,  # type: ignore[arg-type]
        result_type="page" if candidate.asset_type == "PAGE" else "image",
        page_number=candidate.page_number,
        content_type=candidate.content_type,
        snippet=item.snippet,
        score=item.score,
        matched_lanes=list(item.matched_lanes),
        preview_available=True,
    )


def search(
    db: Session,
    user: models.User,
    *,
    query: str,
    mode: str,
    limit: int,
    allowed_ids: frozenset[int] | set[int] | None,
) -> schemas.VisualSearchResponse:
    """Search authorized visual assets and reauthorize before returning hits."""

    if not settings.visual_search_enabled:
        return schemas.VisualSearchResponse(
            query=query,
            mode=mode,  # type: ignore[arg-type]
            count=0,
            hits=[],
            provider="disabled",
            degraded=True,
        )
    if not 1 <= limit <= 100:
        raise ValueError("visual search limit out of range")
    normalized = query.strip()
    if not normalized:
        return schemas.VisualSearchResponse(
            query="",
            mode=mode,  # type: ignore[arg-type]
            count=0,
            hits=[],
            provider="visual_text",
        )

    initial_allowed = frozenset(allowed_ids or ())
    if not initial_allowed:
        return schemas.VisualSearchResponse(
            query=normalized,
            mode=mode,  # type: ignore[arg-type]
            count=0,
            hits=[],
            provider="visual_text",
        )

    semantic_response = _semantic_search(
        db,
        user,
        query=normalized,
        mode=mode,
        limit=limit,
        allowed_ids=initial_allowed,
    )
    if semantic_response is not None:
        return semantic_response

    candidates = visual_search_repository.list_candidates(
        db,
        document_ids=initial_allowed,
        asset_types=_asset_types(mode),
        limit=settings.visual_search_max_candidates,
    )
    query_tokens = _tokens(normalized)
    scored_by_asset: dict[int, _ScoredCandidate] = {}
    for candidate in candidates:
        result = _score_candidate(candidate, normalized, query_tokens)
        if result is not None:
            scored_by_asset[candidate.asset_id] = result

    def lane_searcher(
        lane: VisualLane,
    ):
        def search_lane(
            _query: str,
            lane_allowed_ids: frozenset[int],
            budget: int,
        ) -> list[dict]:
            lane_results: list[dict] = []
            for item in scored_by_asset.values():
                candidate = item.candidate
                if candidate.document_id not in lane_allowed_ids:
                    continue
                if lane is VisualLane.PAGE and candidate.asset_type != "PAGE":
                    continue
                if lane is VisualLane.IMAGE and candidate.asset_type not in {"IMAGE", "REGION"}:
                    continue
                lane_results.append(
                    {
                        "asset_id": candidate.asset_id,
                        "document_id": candidate.document_id,
                        "version_id": candidate.version_id,
                        "result_type": "page" if candidate.asset_type == "PAGE" else "image",
                        "score": item.score,
                    }
                )
            lane_results.sort(key=lambda row: (-float(row["score"]), int(row["asset_id"])))
            return lane_results[:budget]

        return search_lane

    routed = route_typed_query(
        mode=VisualQueryMode(mode),
        query=normalized,
        allowed_ids=initial_allowed,
        limit=limit,
        lane_searchers={
            VisualLane.PAGE: lane_searcher(VisualLane.PAGE),
            VisualLane.IMAGE: lane_searcher(VisualLane.IMAGE),
        },
        budgets=LaneBudget(
            page=settings.visual_lane_candidate_budget,
            image=settings.visual_lane_candidate_budget,
            text=settings.visual_lane_candidate_budget,
        ),
        reranker_enabled=settings.visual_reranker_enabled,
    )
    scored: list[_ScoredCandidate] = []
    for row in routed:
        item = scored_by_asset.get(row.get("asset_id"))
        if item is None:
            continue
        lanes = row.get("matched_lanes")
        matched_lanes = (
            tuple(value for value in lanes if isinstance(value, str))
            if isinstance(lanes, list)
            else item.matched_lanes
        )
        scored.append(
            _ScoredCandidate(
                candidate=item.candidate,
                score=item.score,
                snippet=item.snippet,
                matched_lanes=matched_lanes or item.matched_lanes,
            )
        )

    # A query can race an ACL revoke.  Resolve the parent document set again,
    # then use the same active-asset boundary as preview hydration.
    current_allowed = resolve_view_document_ids_for_user_id(db, user.id)
    asset_ids = {
        item.candidate.asset_id
        for item in scored
        if item.candidate.document_id in current_allowed
    }
    authorized_assets = visual_access.reauthorize_visual_assets(
        db,
        user_id=user.id,
        asset_ids=asset_ids,
    )
    authorized_by_id = {asset.id: asset for asset in authorized_assets}
    hits: list[schemas.VisualSearchHit] = []
    for item in scored:
        asset = authorized_by_id.get(item.candidate.asset_id)
        if asset is None or asset.document_id not in current_allowed:
            continue
        hits.append(_to_hit(item))

    return schemas.VisualSearchResponse(
        query=normalized,
        mode=mode,  # type: ignore[arg-type]
        count=len(hits),
        hits=hits,
        provider="visual_text",
        degraded=False,
    )


__all__ = ["search"]
