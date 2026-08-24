"""Search — FR-IDX-02 (keyword) and FR-IDX-03 (semantic, fallback to FTS).

Both endpoints security-trim results to what the caller may VIEW.
OKF bundle management endpoints are included here (admin-only).
"""

from __future__ import annotations

from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from pydantic import BaseModel
from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import require, require_global
from ..mongodb import get_db as get_mongo_db
from ..repositories import ask_ai_repository
from ..services import (
    okf_service,
    rbac_service,
    search_application_service,
    search_authorization,
    visual_access,
    visual_assets,
    visual_query_service,
    visual_telemetry,
)
from ..services.ask_ai.mongo_models import ActiveScope
from ..utils.request_context import get_request_context

router = APIRouter(prefix="/api/v1/search", tags=["search"])


async def _read_bounded_upload(image: UploadFile, *, max_bytes: int) -> bytes:
    """Read an upload incrementally so an ephemeral query cannot grow unbounded."""

    if max_bytes < 1:
        raise ValueError("upload byte budget is invalid")
    buffer = bytearray()
    while True:
        chunk = await image.read(min(1024 * 1024, max_bytes - len(buffer) + 1))
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ValueError("image query exceeds byte budget")
    return bytes(buffer)


@router.post("/image")
async def image_search(
    image: UploadFile = File(...),
    limit: int = Query(default=20, ge=1, le=100),
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Process an image query ephemerally; visual indexes remain opt-in."""
    try:
        payload = await _read_bounded_upload(image, max_bytes=settings.max_upload_bytes)
        result = visual_query_service.run_ephemeral_image_query(
            payload,
            image.content_type or "application/octet-stream",
            limit=limit,
            authorized_ids=frozenset(search_authorization.resolve_view_document_ids(db, user)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid image query") from exc
    finally:
        payload = b""
    return {
        "mode": "image_to_image",
        "count": result.count,
        "hits": result.hits,
        "provider": result.provider,
        "audit": result.audit,
        "ephemeral": True,
    }


@router.get("/visual-assets/{asset_id}/preview")
def visual_asset_preview(
    asset_id: int,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Stream a derivative only after fresh SQL document authorization."""
    try:
        preview = visual_access.open_authorized_preview(
            db, user_id=user.id, asset_id=asset_id
        )
    except visual_access.VisualAccessDenied as exc:
        raise HTTPException(status_code=404, detail="visual preview not found") from exc
    return StreamingResponse(
        BytesIO(preview.body),
        media_type=preview.content_type,
        headers={"Content-Disposition": f'inline; filename="{preview.filename}"'},
    )


@router.post("/visual-feedback")
def visual_feedback(
    payload: schemas.VisualFeedbackRequest,
    user: models.User = Depends(require("VIEW")),
):
    """Accept bounded feedback without storing result content or notes."""
    del user
    feedback = visual_telemetry.VisualFeedback(
        payload.result_id, payload.category, payload.note
    )
    return {"accepted": True, "feedback": feedback.audit_fields()}


@router.get("", response_model=schemas.SearchResponse)
def keyword_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=2000),
    limit: int = Query(default=20, ge=1, le=100),
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    return search_application_service.run_search(
        db,
        user,
        query=q,
        allowed_ids=set(search_authorization.resolve_view_document_ids(db, user)),
        limit=limit,
        mode="keyword",
        context=get_request_context(request),
    )


@router.post("/semantic", response_model=schemas.SearchResponse)
def semantic_search(
    payload: schemas.SemanticQuery,
    request: Request,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Natural-language search.

    When transformer embeddings are enabled it would vector-search; in this
    local slice it falls back to the FTS keyword index while preserving the
    same request/response contract (mode reported honestly).
    """
    return search_application_service.run_search(
        db,
        user,
        query=payload.q,
        allowed_ids=set(search_authorization.resolve_view_document_ids(db, user)),
        limit=payload.limit,
        mode="semantic-fallback",
        context=get_request_context(request),
    )


@router.post("/visual", response_model=schemas.VisualSearchResponse)
def visual_search(
    payload: schemas.VisualSearchQuery,
    request: Request,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Search authorized image assets or rendered visual document pages by text."""

    return search_application_service.run_visual_search(
        db,
        user,
        query=payload.q,
        mode=payload.mode,
        allowed_ids=search_authorization.resolve_view_document_ids(db, user),
        limit=payload.limit,
        context=get_request_context(request),
    )


@router.post("/visual/image", response_model=schemas.VisualSearchResponse)
async def visual_image_search(
    request: Request,
    image: UploadFile = File(...),
    mode: Literal["image_to_image", "hybrid"] = Query(default="image_to_image"),
    limit: int = Query(default=20, ge=1, le=100),
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Search semantically similar authorized visual assets/pages.

    The uploaded query image is validated, normalized, embedded in memory, and
    discarded.  It is never inserted into a visual asset or vector table.
    """

    normalized = b""
    try:
        payload = await _read_bounded_upload(image, max_bytes=settings.max_upload_bytes)
        content_type = image.content_type or "application/octet-stream"
        visual_assets.validate_visual_bytes_isolated(payload, content_type)
        normalized = visual_assets.normalize_visual_derivative_isolated(
            payload,
            content_type,
            output_format="PNG",
            max_output_bytes=settings.max_upload_bytes,
        )
        return search_application_service.run_visual_image_search(
            db,
            user,
            payload=normalized,
            mode=mode,
            allowed_ids=search_authorization.resolve_view_document_ids(db, user),
            limit=limit,
            context=get_request_context(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid image query") from exc
    finally:
        payload = b""
        normalized = b""


from fastapi.responses import FileResponse, StreamingResponse

@router.post("/ask")
async def ask(
    payload: schemas.AskQuery,
    request: Request,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Ask a natural-language question; get a grounded answer with citations (RAG).

    Retrieval is security-trimmed to what the user may VIEW, so the AI answer
    never draws on documents the user cannot access. Returns an SSE stream for multiple models.
    """
    stream_gen = search_application_service.ask_stream(
        db,
        user,
        question=payload.question,
        allowed_ids=set(search_authorization.resolve_view_document_ids(db, user)),
        document_id=payload.document_id,
        history=payload.history,
        conversation_id=payload.conversation_id,
        company_kb_enabled=payload.company_kb_enabled,
        google_drive_enabled=payload.google_drive_enabled,
        model_selections=payload.models,
        passed_answers=payload.passed_answers,
        rerun=payload.rerun,
        context=get_request_context(request),
    )
    return StreamingResponse(stream_gen, media_type="text/event-stream")


@router.get("/ask/models")
def ask_models(user: models.User = Depends(require("VIEW"))):
    """The Ask AI model registry: configured providers, versions, capabilities, pricing."""
    from ..services.ask_ai import model_registry
    from ..services.ask_ai.multi_llm import get_configured_providers

    return {"providers": model_registry.to_public(model_registry.available_providers(get_configured_providers()))}


@router.get("/ask/models/live")
async def ask_models_live(provider: str | None = None, user: models.User = Depends(require("VIEW"))):
    """Query live provider APIs directly to list available models for configured keys."""
    from ..services.ask_ai import model_registry

    return {"live_models": await model_registry.fetch_live_models(provider)}


@router.get("/ask/usage")
async def ask_usage(user: models.User = Depends(require("VIEW"))):
    """Today's Ask AI usage vs the configured budgets (0 = unlimited)."""
    from ..mongodb import get_db as _mongo
    from ..repositories import ask_ai_repository

    usage = await ask_ai_repository.get_daily_usage(_mongo(), user.id)
    return {
        "usage": usage,
        "limits": {
            "daily_cost_usd": settings.ask_ai_daily_cost_limit_usd,
            "daily_tokens": settings.ask_ai_daily_token_limit,
            "daily_sandbox_execs": settings.ask_ai_daily_sandbox_limit,
            "max_concurrent_runs": settings.ask_ai_max_concurrent_runs,
        },
        "tools_enabled": settings.ask_ai_tools_enabled,
        "sandbox_enabled": settings.ask_ai_sandbox_enabled,
    }


@router.get("/ask/artifacts/{artifact_id}")
async def ask_artifact_download(
    artifact_id: str,
    user: models.User = Depends(require("VIEW")),
):
    """Download an Ask AI generated artifact (owner-only)."""
    from ..mongodb import get_db as get_mongo_db
    from ..services.ask_ai import artifact_store

    meta = await artifact_store.get_artifact(get_mongo_db(), artifact_id, user.id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(
        meta["path"],
        media_type=meta["mime"],
        filename=meta["name"],
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/ask/artifacts/{artifact_id}/preview")
async def ask_artifact_preview(
    artifact_id: str,
    user: models.User = Depends(require("VIEW")),
):
    """Inline preview. Generated HTML is served fully sandboxed via CSP —
    no scripts' network egress, no cookies, isolated origin semantics."""
    from ..mongodb import get_db as get_mongo_db
    from ..services.ask_ai import artifact_store

    meta = await artifact_store.get_artifact(get_mongo_db(), artifact_id, user.id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    headers = {"X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"}
    if meta["mime"] == "text/html":
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data:; font-src data:"
        )
    return FileResponse(meta["path"], media_type=meta["mime"], headers=headers)


class SelectAnswerQuery(BaseModel):
    conversation_id: str
    chosen_answer: str
    provider: str
    model_id: str | None = None
    metrics: dict | None = None

@router.post("/ask/select")
async def ask_select(
    payload: SelectAnswerQuery,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Save the chosen parallel LLM answer."""
    return await search_application_service.select_answer(
        conversation_id=payload.conversation_id,
        user=user,
        chosen_answer=payload.chosen_answer,
        provider=payload.provider,
        model_id=payload.model_id,
        metrics=payload.metrics,
    )


# ── Ask AI Conversation Management ───────────────────────────────────────────


@router.get("/conversations", response_model=list[schemas.ConversationOut])
async def list_conversations(
    user: models.User = Depends(require("VIEW")),
):
    """List recent Ask AI conversations for the current user."""
    mongo_db = get_mongo_db()
    if mongo_db is None:
        return []
    convs = await ask_ai_repository.list_conversations(mongo_db, user.id)
    return [
        schemas.ConversationOut(
            id=c["_id"],
            title=c.get("title", "New Conversation"),
            created_at=c.get("created_at"),
            last_message_at=c.get("last_message_at"),
            company_kb_enabled=c.get("company_kb_enabled", True),
            google_drive_enabled=c.get("google_drive_enabled", False),
            parent_ids=c.get("parent_ids") or [],
            branched_from=c.get("branched_from") or [],
        )
        for c in convs
    ]


@router.post("/conversations", response_model=schemas.ConversationOut)
async def create_conversation(
    payload: schemas.ConversationCreateIn | None = None,
    user: models.User = Depends(require("VIEW")),
):
    """Create a new Ask AI conversation session."""
    mongo_db = get_mongo_db()
    if mongo_db is None:
        raise HTTPException(
            status_code=503,
            detail="Conversation persistence is disabled (MongoDB not configured).",
        )
    await ask_ai_repository.upsert_mongo_user(mongo_db, user.id, user.name, user.email)
    kb_enabled = payload.company_kb_enabled if payload else True
    drive_enabled = payload.google_drive_enabled if payload else False
    conv_id = await ask_ai_repository.create_conversation(
        mongo_db,
        user.id,
        company_kb_enabled=kb_enabled,
        google_drive_enabled=drive_enabled,
    )
    if not conv_id:
        raise HTTPException(status_code=500, detail="Failed to create conversation.")
    conv = await ask_ai_repository.get_conversation(mongo_db, conv_id, user.id)
    if not conv:
        raise HTTPException(status_code=500, detail="Failed to retrieve created conversation.")
    return schemas.ConversationOut(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        last_message_at=conv.last_message_at,
        company_kb_enabled=conv.company_kb_enabled,
        google_drive_enabled=conv.google_drive_enabled,
    )


@router.get("/conversations/{conversation_id}", response_model=schemas.ConversationDetailOut)
async def get_conversation(
    conversation_id: str,
    user: models.User = Depends(require("VIEW")),
):
    """Get conversation details including active scope and message history."""
    mongo_db = get_mongo_db()
    if mongo_db is None:
        raise HTTPException(
            status_code=503,
            detail="Conversation persistence is disabled (MongoDB not configured).",
        )
    conv = await ask_ai_repository.get_conversation(mongo_db, conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    history_docs = await ask_ai_repository.get_history_window(
        mongo_db, conversation_id, scope_start_message_id=None, limit=100
    )

    messages = [
        schemas.ConversationMessageOut(
            id=m["_id"],
            role=m["role"],
            content=m["content"],
            created_at=m["created_at"],
            sources_used=m.get("sources_used"),
            provider=m.get("provider"),
            model_id=m.get("model_id"),
            evidence=m.get("evidence"),
        )
        for m in history_docs
    ]

    active_scope_info = schemas.ActiveScopeInfo(
        documents=[
            schemas.ScopedDocumentInfo(document_id=d.document_id, title=d.title)
            for d in conv.active_scope.documents
        ],
        classes=[
            schemas.ScopedClassInfo(
                class_id=c.class_id,
                class_name=c.class_name,
                document_ids=c.document_ids,
            )
            for c in conv.active_scope.classes
        ],
    )

    return schemas.ConversationDetailOut(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        last_message_at=conv.last_message_at,
        active_scope=active_scope_info,
        company_kb_enabled=conv.company_kb_enabled,
        google_drive_enabled=conv.google_drive_enabled,
        parent_ids=conv.parent_ids,
        branched_from=conv.branched_from,
        enabled_models=conv.enabled_models,
        messages=messages,
    )


@router.delete("/conversations/{conversation_id}/scope")
async def clear_conversation_scope(
    conversation_id: str,
    user: models.User = Depends(require("VIEW")),
):
    """Clear active document and class filters for a conversation."""
    mongo_db = get_mongo_db()
    if mongo_db is None:
        return {"cleared": True}
    conv = await ask_ai_repository.get_conversation(mongo_db, conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    await ask_ai_repository.update_conversation_scope(
        mongo_db, conversation_id, user.id, ActiveScope(), None
    )
    return {"cleared": True}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user: models.User = Depends(require("VIEW")),
):
    """Delete a conversation and its messages."""
    mongo_db = get_mongo_db()
    if mongo_db is None:
        return {"deleted": True}
    success = await ask_ai_repository.delete_conversation(mongo_db, conversation_id, user.id)
    if not success:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"deleted": True}



# ── OKF bundle management ─────────────────────────────────────────────────────


@router.post("/conversations/{conversation_id}/branch", response_model=schemas.BranchOut)
async def branch_conversation(
    conversation_id: str,
    payload: schemas.BranchRequest,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """'Continue with {model}': accept picked answer(s) and fork a child conversation."""
    from ..services.exceptions import NotFoundError, ServiceError

    try:
        return await search_application_service.branch_conversation(db, user, conversation_id, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/conversations/{conversation_id}/runs")
async def conversation_runs(
    conversation_id: str,
    user: models.User = Depends(require("VIEW")),
):
    """Comparison log: every persisted model run of a conversation."""
    from ..mongodb import get_db as _mongo
    from ..repositories import ask_ai_repository

    runs = await ask_ai_repository.list_turn_runs(_mongo(), conversation_id, user.id)
    return {
        "runs": [
            {
                "id": r.get("_id"),
                "turn_message_id": r.get("turn_message_id"),
                "provider": r.get("provider"),
                "model_id": r.get("model_id"),
                "display_version": r.get("display_version"),
                "reasoning": r.get("reasoning", "none"),
                "status": r.get("status"),
                "body": r.get("body", ""),
                "selected": bool(r.get("selected")),
                "passed_from": r.get("passed_from", []),
                "tool_events": r.get("tool_events", []),
                "artifacts": r.get("artifacts", []),
                "metrics": r.get("metrics", {}),
                "created_at": r.get("created_at"),
            }
            for r in runs
        ]
    }


@router.get("/okf/status", tags=["okf"])
def okf_status(
    user: models.User = Depends(require("VIEW")),
):
    """Return the status of the Open Knowledge Format bundle."""
    return okf_service.bundle_status()


@router.get("/okf/entries", tags=["okf"])
def list_okf_entries(
    user: models.User = Depends(require("VIEW")),
):
    """List all OKF bundle entries (title, category, tags)."""
    return okf_service.list_entry_summaries()


@router.post("/okf/entries", status_code=201, tags=["okf"])
def create_okf_entry(
    request: Request,
    payload: schemas.OkfEntryCreate,
    user: models.User = Depends(
        require_global(rbac_service.MANAGE_KNOWLEDGE_PERMISSION)
    ),
    db: Session = Depends(get_db),
):
    """Add or replace a Markdown entry in the OKF bundle.

    The file must include YAML frontmatter with at minimum a `title` field.
    On success the bundle is reloaded from disk.
    """
    return search_application_service.create_okf_entry(
        db,
        user,
        filename=payload.filename,
        content=payload.content,
        context=get_request_context(request),
    )


@router.post("/okf/reload", tags=["okf"])
def reload_okf_bundle(
    request: Request,
    user: models.User = Depends(
        require_global(rbac_service.MANAGE_KNOWLEDGE_PERMISSION)
    ),
    db: Session = Depends(get_db),
):
    """Force-reload the OKF bundle from disk (useful after manual edits)."""
    count = search_application_service.reload_okf_bundle(
        db,
        user,
        context=get_request_context(request),
    )
    return {"status": "reloaded", "entry_count": count}
