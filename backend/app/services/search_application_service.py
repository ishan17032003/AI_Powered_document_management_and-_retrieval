"""Request-level search, RAG, and OKF use cases."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .. import models, schemas
from ..observability import sensitive_query_telemetry, trace_span
from ..config import settings
from ..mongodb import get_db as get_mongo_db
from ..repositories import ask_ai_repository, visual_search_repository
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
from .ask_ai import (
    answer_synthesis,
    company_kb_agent,
    conversation_router,
    google_drive_agent,
)
from .ask_ai.mention_parser import parse_mentions
from .ask_ai.mongo_models import ActiveScope, ScopedDocument, SourcesUsed
from .ask_ai.scope_manager import compute_next_scope
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


def _to_active_scope_info(scope: ActiveScope) -> dict:
    return {
        "documents": [
            {"document_id": d.document_id, "title": d.title}
            for d in scope.documents
        ],
        "classes": [
            {
                "class_id": c.class_id,
                "class_name": c.class_name,
                "document_ids": list(c.document_ids or []),
            }
            for c in scope.classes
        ],
    }


async def ask(
    db: Session,
    user: models.User,
    *,
    question: str,
    allowed_ids: set[int] | None,
    document_id: int | None = None,
    history: list[schemas.ChatMessage] | None = None,
    conversation_id: str | None = None,
    company_kb_enabled: bool = True,
    google_drive_enabled: bool = False,
    context: RequestContext | None = None,
) -> schemas.AskResponse:
    normalized = question.strip()
    if not normalized:
        return schemas.AskResponse(
            question="",
            answer="Please enter a question.",
            mode="extractive",
        )

    # Source selection safety check: at least one source must be enabled
    if not company_kb_enabled and not google_drive_enabled:
        company_kb_enabled = True

    request_context = context_with_document(
        context_with_actor(context, user.id),
        document_id,
    )

    mongo_db = get_mongo_db()
    if mongo_db is not None:
        await ask_ai_repository.upsert_mongo_user(
            mongo_db, user.id, user.name, user.email
        )

    # 1. Parse mentions (@doc:{...}, @class:{...}, @drive)
    mentions = parse_mentions(normalized)
    clean_query = mentions.get("clean_query") or normalized
    if mentions.get("drive"):
        google_drive_enabled = True

    # 2. Scope Management & Conversation Session
    active_scope = ActiveScope()
    conv = None
    scope_changed = False

    if conversation_id and mongo_db is not None:
        conv = await ask_ai_repository.get_conversation(mongo_db, conversation_id, user.id)
        if conv:
            active_scope = conv.active_scope
    elif mongo_db is not None:
        conversation_id = await ask_ai_repository.create_conversation(
            mongo_db,
            user.id,
            company_kb_enabled=company_kb_enabled,
            google_drive_enabled=google_drive_enabled,
        )
        if conversation_id:
            conv = await ask_ai_repository.get_conversation(mongo_db, conversation_id, user.id)
            if conv:
                active_scope = conv.active_scope


    # Compute next scope based on mentions and caller's RBAC allowed_ids
    active_scope, scope_changed = compute_next_scope(
        active_scope, mentions, db, allowed_ids
    )

    # If an explicit document_id was provided (e.g. from document detail page)
    if document_id is not None and not any(d.document_id == document_id for d in active_scope.documents):
        from ..repositories import document_repository
        doc_row = document_repository.get(db, document_id)
        if doc_row:
            active_scope.documents.append(
                ScopedDocument(document_id=doc_row.id, title=doc_row.title)
            )

    # 3. Intent Classification
    intent = conversation_router.classify(normalized, active_scope)

    with bound_request_context(request_context):
        # ── Case A: Greeting ──────────────────────────────────────────────────
        if intent == "greeting":
            greeting_answer = (
                "Hello! I am DocVault's Ask AI assistant. I can help you search, "
                "summarize, and answer questions across your authorized documents and connected sources. "
                "What would you like to know?"
            )
            if conversation_id and mongo_db is not None:
                sources_used = SourcesUsed(company_kb=False, google_drive=False)
                await ask_ai_repository.append_message(
                    mongo_db, conversation_id, user.id, role="user",
                    content=normalized, scope_snapshot=active_scope, sources_used=sources_used
                )
                await ask_ai_repository.append_message(
                    mongo_db, conversation_id, user.id, role="assistant",
                    content=greeting_answer, scope_snapshot=active_scope, sources_used=sources_used
                )
                from datetime import datetime, timezone
                await ask_ai_repository.update_conversation_meta(
                    mongo_db, conversation_id, user.id, last_message_at=datetime.now(timezone.utc)
                )

            return schemas.AskResponse(
                question=normalized,
                answer=greeting_answer,
                mode="greeting",
                conversation_id=conversation_id,
                active_scope=_to_active_scope_info(active_scope),
                sources_used={"company_kb": False, "google_drive": False},
                drive_sources=[],
            )

        # ── Case B: Scope Removal ─────────────────────────────────────────────
        if intent == "scope_removal":
            cleared_scope = ActiveScope()
            removal_answer = (
                "Active document and class filters have been removed. "
                "Future questions will search across all accessible documents."
            )
            if conversation_id and mongo_db is not None:
                await ask_ai_repository.update_conversation_scope(
                    mongo_db, conversation_id, user.id, cleared_scope, None
                )
                sources_used = SourcesUsed(company_kb=False, google_drive=False)
                await ask_ai_repository.append_message(
                    mongo_db, conversation_id, user.id, role="user",
                    content=normalized, scope_snapshot=cleared_scope, sources_used=sources_used
                )
                await ask_ai_repository.append_message(
                    mongo_db, conversation_id, user.id, role="assistant",
                    content=removal_answer, scope_snapshot=cleared_scope, sources_used=sources_used
                )

            return schemas.AskResponse(
                question=normalized,
                answer=removal_answer,
                mode="scope_removal",
                conversation_id=conversation_id,
                active_scope=_to_active_scope_info(cleared_scope),
                sources_used={"company_kb": False, "google_drive": False},
                drive_sources=[],
            )

        # ── Case C: RAG Retrieval & Synthesis ─────────────────────────────────
        # Build history window
        rag_history: list[dict] = []
        if conversation_id and mongo_db is not None:
            boundary_id = conv.scope_start_message_id if conv else None
            db_history = await ask_ai_repository.get_history_window(
                mongo_db, conversation_id, boundary_id, limit=settings.ask_ai_max_history_turns
            )
            rag_history = [{"role": m["role"], "content": m["content"]} for m in db_history]
        elif history:
            rag_history = [{"role": m.role, "content": m.content} for m in history]

        # Execute Company KB Agent
        kb_result = None
        if company_kb_enabled:
            with trace_span("fusion", "company_kb_rag", context=request_context):
                kb_result = company_kb_agent.run(
                    db,
                    clean_query,
                    active_scope,
                    allowed_ids,
                    user_id=user.id,
                    history=rag_history if rag_history else None,
                )

        # Execute Google Drive Agent
        drive_files: list[dict] = []
        if google_drive_enabled:
            with trace_span("fusion", "google_drive_rag", context=request_context):
                drive_token = None
                if mongo_db is not None:
                    m_user = await ask_ai_repository.get_mongo_user(mongo_db, user.id)
                    if m_user and m_user.google_drive_token:
                        drive_token = m_user.google_drive_token
                drive_files = await google_drive_agent.run(
                    clean_query,
                    mentions,
                    drive_token=drive_token,
                )

        # Synthesize Final Answer
        with trace_span("fusion", "answer_synthesis", context=request_context):
            synth_res = await answer_synthesis.synthesize(
                clean_query,
                rag_history,
                kb_result,
                drive_files,
            )

        # Find visual figures/diagrams if KB produced results
        images = []
        if kb_result is not None:
            with trace_span("retrieval", "ask_images", context=request_context):
                images = _ask_images(
                    db,
                    user,
                    question=clean_query,
                    allowed_ids=allowed_ids,
                    document_id=document_id,
                    result=kb_result,
                )

        # Persist conversation turn in MongoDB
        if conversation_id and mongo_db is not None:
            sources_used = SourcesUsed(
                company_kb=bool(company_kb_enabled and kb_result and kb_result.mode != "notfound"),
                google_drive=bool(google_drive_enabled and drive_files),
            )
            user_msg_id = await ask_ai_repository.append_message(
                mongo_db,
                conversation_id,
                user.id,
                role="user",
                content=normalized,
                scope_snapshot=active_scope,
                sources_used=sources_used,
            )
            await ask_ai_repository.append_message(
                mongo_db,
                conversation_id,
                user.id,
                role="assistant",
                content=synth_res.answer,
                scope_snapshot=active_scope,
                sources_used=sources_used,
            )

            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc)
            if scope_changed:
                await ask_ai_repository.update_conversation_scope(
                    mongo_db, conversation_id, user.id, active_scope, user_msg_id
                )

            # Auto-title conversation from first query if still default
            if conv and (not conv.title or conv.title == "New Conversation"):
                await ask_ai_repository.update_conversation_meta(
                    mongo_db, conversation_id, user.id, title=clean_query[:60], last_message_at=now_utc
                )
            else:
                await ask_ai_repository.update_conversation_meta(
                    mongo_db, conversation_id, user.id, last_message_at=now_utc
                )

        # Record audit log
        citations = synth_res.citations or []
        audit_service.record(
            db,
            actor=user,
            action="ASK",
            object_type="query",
            object_id=normalized[:60],
            details={
                "mode": synth_res.mode,
                "scoped_docs": len(active_scope.documents),
                "scoped_classes": len(active_scope.classes),
                "citations": len(citations),
                "drive_files": len(drive_files),
                "images": len(images),
                "conversation_id": conversation_id,
                **sensitive_query_telemetry(normalized),
            },
            context=request_context,
        )

    # Map citations to schema
    parsed_citations = []
    for item in citations:
        if isinstance(item, dict):
            parsed_citations.append(schemas.Citation(**item))
        elif hasattr(item, "document_id"):
            parsed_citations.append(
                schemas.Citation(
                    index=getattr(item, "index", 1),
                    document_id=item.document_id,
                    title=getattr(item, "title", "Document"),
                )
            )

    candidates = [
        schemas.Candidate(**item) if isinstance(item, dict) else schemas.Candidate(document_id=item.document_id, title=item.title)
        for item in (kb_result.candidates if kb_result else [])
    ]

    drive_citations = [
        schemas.DriveSourceCitation(**ds) for ds in synth_res.drive_sources
    ]

    scoped_doc_id = (
        active_scope.documents[0].document_id
        if len(active_scope.documents) == 1 and not active_scope.classes
        else document_id
    )

    return schemas.AskResponse(
        question=normalized,
        answer=synth_res.answer,
        mode=synth_res.mode,
        model=kb_result.model if kb_result else None,
        needs_clarification=kb_result.needs_clarification if kb_result else False,
        scoped_document_id=scoped_doc_id,
        citations=parsed_citations,
        candidates=candidates,
        images=images,
        conversation_id=conversation_id,
        active_scope=_to_active_scope_info(active_scope),
        sources_used={"company_kb": bool(company_kb_enabled), "google_drive": bool(google_drive_enabled)},
        drive_sources=drive_citations,
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


async def ask_stream(
    db: Session,
    user: models.User,
    *,
    question: str,
    allowed_ids: set[int] | None,
    document_id: int | None = None,
    history: list[schemas.ChatMessage] | None = None,
    conversation_id: str | None = None,
    company_kb_enabled: bool = True,
    google_drive_enabled: bool = False,
    model_selections: list[schemas.ModelSelection] | None = None,
    context: RequestContext | None = None,
):
    import json
    from datetime import datetime, timezone
    from .ask_ai.multi_llm import synthesize_parallel_stream
    
    normalized = question.strip()
    if not normalized:
        yield 'data: {"type": "done"}\n\n'
        return

    if not company_kb_enabled and not google_drive_enabled:
        company_kb_enabled = True

    request_context = context_with_document(context_with_actor(context, user.id), document_id)
    mongo_db = get_mongo_db()
    if mongo_db is not None:
        await ask_ai_repository.upsert_mongo_user(mongo_db, user.id, user.name, user.email)

    mentions = parse_mentions(normalized)
    clean_query = mentions.get("clean_query") or normalized
    if mentions.get("drive"):
        google_drive_enabled = True

    active_scope = ActiveScope()
    conv = None
    scope_changed = False

    if conversation_id and mongo_db is not None:
        conv = await ask_ai_repository.get_conversation(mongo_db, conversation_id, user.id)
        if conv:
            active_scope = conv.active_scope
    elif mongo_db is not None:
        conversation_id = await ask_ai_repository.create_conversation(
            mongo_db, user.id, company_kb_enabled=company_kb_enabled, google_drive_enabled=google_drive_enabled
        )
        if conversation_id:
            conv = await ask_ai_repository.get_conversation(mongo_db, conversation_id, user.id)
            if conv:
                active_scope = conv.active_scope

    active_scope, scope_changed = compute_next_scope(active_scope, mentions, db, allowed_ids)

    if document_id is not None and not any(d.document_id == document_id for d in active_scope.documents):
        from ..repositories import document_repository
        doc_row = document_repository.get(db, document_id)
        if doc_row:
            active_scope.documents.append(ScopedDocument(document_id=doc_row.id, title=doc_row.title))

    intent = conversation_router.classify(normalized, active_scope)

    if intent in ("greeting", "scope_removal"):
        answer = "Hello! I am DocVault's Ask AI assistant." if intent == "greeting" else "Active document and class filters have been removed."
        yield f'data: {json.dumps({"type": "meta", "conversation_id": conversation_id, "active_scope": _to_active_scope_info(active_scope)})}\n\n'
        yield f'data: {json.dumps({"type": "chunk", "provider": "vllm", "chunk": answer})}\n\n'
        yield 'data: {"type": "done"}\n\n'
        return

    rag_history: list[dict] = []
    if conversation_id and mongo_db is not None:
        boundary_id = conv.scope_start_message_id if conv else None
        db_history = await ask_ai_repository.get_history_window(
            mongo_db, conversation_id, boundary_id, limit=settings.ask_ai_max_history_turns
        )
        rag_history = [{"role": m["role"], "content": m["content"]} for m in db_history]
    elif history:
        rag_history = [{"role": m.role, "content": m.content} for m in history]

    kb_result = None
    if company_kb_enabled:
        kb_result = company_kb_agent.run(
            db, clean_query, active_scope, allowed_ids, user_id=user.id, history=rag_history if rag_history else None
        )

    drive_files: list[dict] = []
    if google_drive_enabled:
        drive_token = None
        if mongo_db is not None:
            m_user = await ask_ai_repository.get_mongo_user(mongo_db, user.id)
            if m_user and m_user.google_drive_token:
                drive_token = m_user.google_drive_token
        drive_files = await google_drive_agent.run(clean_query, mentions, drive_token=drive_token)

    images = []
    if kb_result is not None:
        images = _ask_images(db, user, question=clean_query, allowed_ids=allowed_ids, document_id=document_id, result=kb_result)

    sources_used = SourcesUsed(
        company_kb=bool(company_kb_enabled and kb_result and kb_result.mode != "notfound"),
        google_drive=bool(google_drive_enabled and drive_files),
    )

    user_msg_id: str | None = None
    if conversation_id and mongo_db is not None:
        user_msg_id = await ask_ai_repository.append_message(
            mongo_db, conversation_id, user.id, role="user", content=normalized, scope_snapshot=active_scope, sources_used=sources_used
        )
        if scope_changed:
            await ask_ai_repository.update_conversation_scope(mongo_db, conversation_id, user.id, active_scope, user_msg_id)
        if conv and (not conv.title or conv.title == "New Conversation"):
            await ask_ai_repository.update_conversation_meta(mongo_db, conversation_id, user.id, title=clean_query[:60], last_message_at=datetime.now(timezone.utc))

    citations = [c.model_dump() if hasattr(c, "model_dump") else (dict(c) if hasattr(c, "__iter__") else c) for c in (kb_result.citations or [])] if kb_result else []
    drive_sources = [
        {"id": f.get("id"), "name": f.get("name"), "webViewLink": f.get("webViewLink", "")}
        for f in (drive_files or [])
    ]
    images_payload = [img.model_dump() if hasattr(img, "model_dump") else dict(img) for img in images]

    from .ask_ai.multi_llm import synthesize_parallel_stream, get_configured_providers
    from .ask_ai import model_registry
    configured_providers = get_configured_providers()
    if model_selections is not None:
        selections = {
            m.provider: {"model_id": m.model_id, "reasoning": m.reasoning}
            for m in model_selections
            if m.provider in configured_providers
        }
        if not selections:
            selections = {p: {} for p in configured_providers}
    else:
        selections = {p: {} for p in configured_providers}
    registry_public = model_registry.to_public(model_registry.available_providers(configured_providers))

    yield f'data: {json.dumps({"type": "meta", "conversation_id": conversation_id, "active_scope": _to_active_scope_info(active_scope), "citations": citations, "drive_sources": drive_sources, "images": images_payload, "providers": configured_providers, "models": registry_public}, default=str)}\n\n'

    # Build prompt for providers
    system_prompt = (
        "You are DocVault AI. Answer based on the provided context.\n"
        "You may call the read-only tools search_documents (re-query the vault "
        "for passages) and list_documents (catalogue lookup) when the provided "
        "context is insufficient or a claim needs verification. Results are "
        "limited to documents the user is authorized to view; cite what you use.\n"
    )
    if kb_result and kb_result.mode != "notfound":
        system_prompt += f"\nCompany KB Context:\n{kb_result.answer}\n"
    if drive_files:
        for i, f in enumerate(drive_files, 1):
            system_prompt += f"\nDrive File {i} ({f.get('name')}):\n{f.get('content', '')[:3500]}\n"
    
    provider_messages = [{"role": "system", "content": system_prompt}] + rag_history + [{"role": "user", "content": clean_query}]

    # Tools: authorized retrieval/catalogue re-query, scoped exactly like the
    # chat context (conversation scope ∩ caller's VIEW set).
    from .ask_ai.tools import ToolContext
    tools_ctx = ToolContext(
        db, user.id, allowed_ids, active_scope,
        mongo_db=mongo_db, conversation_id=conversation_id,
    )

    # Accumulate every model run so the full comparison grid is persisted in
    # Mongo (ask_ai_turn_runs), not just the answer the user later selects.
    run_acc: dict[str, dict] = {}
    async for chunk in synthesize_parallel_stream(provider_messages, selections, tools_ctx):
        try:
            event = json.loads(chunk[6:]) if chunk.startswith("data: ") else None
        except (json.JSONDecodeError, TypeError):
            event = None
        if event:
            etype = event.get("type")
            provider = event.get("provider")
            if etype == "run_started" and provider:
                run_acc[provider] = {
                    "provider": provider,
                    "model_id": event.get("model_id", ""),
                    "display_version": event.get("display_version", ""),
                    "reasoning": event.get("reasoning", "none"),
                    "body": "",
                    "tool_events": [],
                }
            elif etype == "chunk" and provider in run_acc:
                run_acc[provider]["body"] += event.get("chunk", "")
            elif etype == "artifact" and provider in run_acc:
                run_acc[provider].setdefault("artifacts", []).append(
                    {"id": event.get("id"), "name": event.get("name"),
                     "mime": event.get("mime"), "size": event.get("size")}
                )
            elif etype in ("tool_started", "tool_result") and provider in run_acc:
                run_acc[provider]["tool_events"].append(
                    {k: v for k, v in event.items() if k not in ("provider",)}
                )
            elif etype == "run_completed" and provider in run_acc:
                run_acc[provider]["status"] = event.get("status", "ok")
                run_acc[provider]["metrics"] = event.get("metrics") or {}
        yield chunk

    if conversation_id and mongo_db is not None and run_acc:
        await ask_ai_repository.append_turn_runs(
            mongo_db, conversation_id, user.id, user_msg_id or "", list(run_acc.values())
        )


async def select_answer(
    conversation_id: str,
    user: models.User,
    chosen_answer: str,
    provider: str,
    model_id: str | None = None,
    metrics: dict | None = None,
):
    from .ask_ai.mongo_models import RunMetrics

    mongo_db = get_mongo_db()
    if mongo_db is not None:
        from datetime import datetime, timezone
        sources_used = SourcesUsed(company_kb=True, google_drive=False)
        await ask_ai_repository.append_message(
            mongo_db, conversation_id, user.id, role="assistant", content=chosen_answer,
            scope_snapshot=ActiveScope(), sources_used=sources_used,
            provider=provider, model_id=model_id,
            metrics=RunMetrics(**metrics) if metrics else None,
        )
        await ask_ai_repository.mark_run_selected(mongo_db, conversation_id, user.id, provider)
        await ask_ai_repository.update_conversation_meta(mongo_db, conversation_id, user.id, last_message_at=datetime.now(timezone.utc))
    return {"status": "ok"}
