"""Search — FR-IDX-02 (keyword) and FR-IDX-03 (semantic, fallback to FTS).

Both endpoints security-trim results to what the caller may VIEW.
OKF bundle management endpoints are included here (admin-only).
"""

from __future__ import annotations

from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import require, require_global
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


@router.post("/ask", response_model=schemas.AskResponse)
def ask(
    payload: schemas.AskQuery,
    request: Request,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    """Ask a natural-language question; get a grounded answer with citations (RAG).

    Retrieval is security-trimmed to what the user may VIEW, so the AI answer
    never draws on documents the user cannot access.
    """
    return search_application_service.ask(
        db,
        user,
        question=payload.question,
        allowed_ids=set(search_authorization.resolve_view_document_ids(db, user)),
        document_id=payload.document_id,
        context=get_request_context(request),
    )


# ── OKF bundle management ─────────────────────────────────────────────────────


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
