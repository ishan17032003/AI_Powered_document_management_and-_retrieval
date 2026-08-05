"""Request-level search, RAG, and OKF use cases."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .. import models, schemas
from ..observability import sensitive_query_telemetry, trace_span
from ..repositories import visual_search_repository
from ..retrieval_store import RetrievalStoreError
from ..utils.request_context import (
    RequestContext,
    bound_request_context,
    context_with_actor,
    context_with_document,
)
from . import (
    audit_service,
    okf_service,
    rag_service,
    rbac_service,
    search_authorization,
    search_service,
    visual_search_service,
)
from .exceptions import PermissionDeniedError, RetrievalUnavailableError, ServiceError


def _require_manage_knowledge(db: Session, user: models.User) -> None:
    permission = rbac_service.MANAGE_KNOWLEDGE_PERMISSION
    if not rbac_service.has_global_permission(db, user, permission):
        raise PermissionDeniedError(f"Missing required permission: {permission}")


def run_search(
    db: Session,
    user: models.User,
    *,
    query: str,
    allowed_ids: set[int] | None,
    limit: int,
    mode: str,
    context: RequestContext | None = None,
) -> schemas.SearchResponse:
    request_context = context_with_actor(context, user.id)
    with bound_request_context(request_context):
        with trace_span("retrieval", "search", context=request_context):
            try:
                hits, hydrated = search_service.search_with_documents(
                    db, query, allowed_ids, limit=limit
                )
            except RetrievalStoreError as exc:
                raise RetrievalUnavailableError(
                    "the selected retrieval provider is unavailable"
                ) from exc
        # Indexes and hydration run before this check.  Resolve the ACL again
        # immediately before the response/audit so a revoke committed during
        # retrieval cannot leak a hit from the stale prefilter.
        final_allowed_ids = search_authorization.resolve_view_document_ids(db, user)
        hits = [
            hit
            for hit in hits
            if type(hit.get("document_id")) is int
            and hit["document_id"] in final_allowed_ids
        ]
        hydrated = [
            item
            for item in hydrated
            if type(item.get("document_id")) is int
            and item["document_id"] in final_allowed_ids
        ]
        audit_service.record(
            db,
            actor=user,
            action="SEARCH",
            object_type="query",
            object_id=query,
            details={"mode": mode, "hits": len(hits), **sensitive_query_telemetry(query)},
            context=request_context,
        )
    results = [schemas.SearchHit(**item) for item in hydrated]
    response_mode = "keyword" if mode == "keyword" else "semantic"
    return schemas.SearchResponse(
        query=query,
        mode=response_mode,
        count=len(results),
        hits=results,
    )


def run_visual_search(
    db: Session,
    user: models.User,
    *,
    query: str,
    mode: str,
    allowed_ids: frozenset[int] | set[int] | None,
    limit: int,
    context: RequestContext | None = None,
) -> schemas.VisualSearchResponse:
    """Run text-to-page/image retrieval with request-scoped audit context."""

    request_context = context_with_actor(context, user.id)
    with bound_request_context(request_context):
        with trace_span("retrieval", "visual_search", context=request_context):
            result = visual_search_service.search(
                db,
                user,
                query=query,
                mode=mode,
                limit=limit,
                allowed_ids=allowed_ids,
            )
        audit_service.record(
            db,
            actor=user,
            action="SEARCH",
            object_type="visual_query",
            object_id=query,
            details={
                "mode": mode,
                "hits": result.count,
                **sensitive_query_telemetry(query),
            },
            context=request_context,
        )
    return result


def run_visual_image_search(
    db: Session,
    user: models.User,
    *,
    payload: bytes,
    mode: str,
    allowed_ids: frozenset[int] | set[int] | None,
    limit: int,
    context: RequestContext | None = None,
) -> schemas.VisualSearchResponse:
    """Run an ephemeral semantic image query with redacted audit fields."""

    request_context = context_with_actor(context, user.id)
    with bound_request_context(request_context):
        with trace_span("retrieval", "visual_image_search", context=request_context):
            result = visual_search_service.search_image(
                db,
                user,
                payload=payload,
                mode=mode,
                limit=limit,
                allowed_ids=allowed_ids,
            )
        audit_service.record(
            db,
            actor=user,
            action="SEARCH",
            object_type="visual_image_query",
            object_id="[uploaded image]",
            details={
                "mode": mode,
                "hits": result.count if result is not None else 0,
                "ephemeral": True,
            },
            context=request_context,
        )
    if result is not None:
        return result
    return schemas.VisualSearchResponse(
        query="[uploaded image]",
        mode=mode,  # type: ignore[arg-type]
        count=0,
        hits=[],
        provider="siglip2_unavailable",
        degraded=True,
    )


_VISUAL_INTENT_TERMS = frozenset({
    "image", "images", "picture", "pictures", "photo", "photos", "photograph",
    "photographs", "figure", "figures", "chart", "charts", "graph", "graphs",
    "diagram", "diagrams", "screenshot", "screenshots", "logo", "logos",
    "drawing", "drawings", "illustration", "illustrations", "visual", "visuals",
})

_ASK_IMAGE_LIMIT = 5


def _has_visual_intent(question: str) -> bool:
    return not _VISUAL_INTENT_TERMS.isdisjoint(re.findall(r"[a-z]+", question.lower()))


def _ask_images(
    db: Session,
    user: models.User,
    *,
    question: str,
    allowed_ids: set[int] | None,
    document_id: int | None,
    result,
) -> list[schemas.VisualSearchHit]:
    """Attach authorized figure/diagram images (never page renders) to an answer.

    The documents the answer actually cited always win: a question that names
    one PDF only ever shows that PDF's figures. Only a visual question that
    produced no citations at all falls back to searching every authorized
    document. When the question's words don't match any visual text (typical
    for "summarize this"), fall back to the scoped documents' own figures so an
    illustrated PDF still shows its images beside the answer.
    """
    scope = set(allowed_ids or ())
    if document_id is not None:
        scope &= {document_id}
    else:
        cited = {item["document_id"] for item in result.citations}
        if result.scoped_document_id is not None:
            cited.add(result.scoped_document_id)
        if cited:
            scope &= cited
        elif not _has_visual_intent(question):
            return []
    if not scope:
        return []
    try:
        # Only an explicitly visual question ranks figures against the query
        # text. For anything else ("summarize X") the question's words say
        # nothing about which figure matters and only reward incidental word
        # overlap in OCR text, so the cited documents' own figures in page
        # order are the honest choice.
        if _has_visual_intent(question):
            visual = visual_search_service.search(
                db,
                user,
                query=question,
                mode="text_to_image",
                limit=_ASK_IMAGE_LIMIT,
                allowed_ids=scope,
            )
            if visual.hits:
                return visual.hits
        candidates = visual_search_repository.list_candidates(
            db,
            document_ids=frozenset(scope),
            asset_types=frozenset({"IMAGE", "REGION"}),
            limit=200,
        )
    except Exception:
        # Images are a best-effort enrichment; the grounded answer stands alone.
        return []
    candidates.sort(key=lambda c: (c.page_number or 0, c.asset_id))
    return [
        schemas.VisualSearchHit(
            asset_id=candidate.asset_id,
            document_id=candidate.document_id,
            version_id=candidate.version_id,
            title=candidate.title,
            asset_type=candidate.asset_type,
            result_type="image",
            page_number=candidate.page_number,
            content_type=candidate.content_type,
            snippet=(candidate.extraction_text or "")[:160],
            score=0.0,
            matched_lanes=["image"],
        )
        for candidate in candidates[:_ASK_IMAGE_LIMIT]
    ]


def ask(
    db: Session,
    user: models.User,
    *,
    question: str,
    allowed_ids: set[int] | None,
    document_id: int | None,
    context: RequestContext | None = None,
) -> schemas.AskResponse:
    normalized = question.strip()
    if not normalized:
        return schemas.AskResponse(
            question="",
            answer="Please enter a question.",
            mode="extractive",
        )
    request_context = context_with_document(
        context_with_actor(context, user.id),
        document_id,
    )
    with bound_request_context(request_context):
        with trace_span("fusion", "rag_answer", context=request_context, document_id=document_id):
            result = rag_service.ask(
                db, normalized, allowed_ids, document_id=document_id, user_id=user.id
            )
        with trace_span("retrieval", "ask_images", context=request_context):
            images = _ask_images(
                db,
                user,
                question=normalized,
                allowed_ids=allowed_ids,
                document_id=document_id,
                result=result,
            )
        audit_service.record(
            db,
            actor=user,
            action="ASK",
            object_type="query",
            object_id=normalized[:60],
            details={
                "mode": result.mode,
                "scoped": result.scoped_document_id,
                "citations": len(result.citations),
                "images": len(images),
                **sensitive_query_telemetry(normalized),
            },
            context=request_context,
        )
    return schemas.AskResponse(
        question=normalized,
        answer=result.answer,
        mode=result.mode,
        model=result.model,
        needs_clarification=result.needs_clarification,
        scoped_document_id=result.scoped_document_id,
        citations=[schemas.Citation(**item) for item in result.citations],
        candidates=[schemas.Candidate(**item) for item in result.candidates],
        images=images,
    )


def create_okf_entry(
    db: Session,
    user: models.User,
    *,
    filename: str,
    content: str,
    context: RequestContext | None = None,
) -> dict:
    request_context = context_with_actor(context, user.id)
    with bound_request_context(request_context):
        _require_manage_knowledge(db, user)
        try:
            result = okf_service.create_entry(filename, content)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        audit_service.record(
            db,
            actor=user,
            action="OKF_ENTRY_SAVE",
            object_type="okf",
            object_id=result["filename"],
            details={"title": result["title"]},
            context=request_context,
        )
    return result


def reload_okf_bundle(
    db: Session,
    user: models.User,
    *,
    context: RequestContext | None = None,
) -> int:
    request_context = context_with_actor(context, user.id)
    with bound_request_context(request_context):
        _require_manage_knowledge(db, user)
        count = okf_service.reload_bundle()
        audit_service.record(
            db,
            actor=user,
            action="OKF_BUNDLE_RELOAD",
            object_type="okf",
            details={"entry_count": count},
            context=request_context,
        )
    return count
