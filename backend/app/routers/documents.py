"""HTTP routes for document capture and retrieval."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Path,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import require, require_any_global
from ..services import document_service, search_authorization
from ..services.rbac_service import IMPORT_SERVER_FOLDER_PERMISSION
from ..utils.request_context import bound_request_context, get_request_context

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


def _decode_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    if len(cursor) > 128:
        raise ValueError("cursor is too long")
    try:
        encoded = cursor.encode()
        encoded += b"=" * (-len(encoded) % 4)
        value = int(base64.urlsafe_b64decode(encoded).decode())
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
    if value <= 0:
        raise ValueError("invalid cursor")
    return value


def _encode_cursor(value: int) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).decode().rstrip("=")


@router.post(
    "",
    response_model=schemas.UploadResult,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    folder_id: int | None = Query(default=None, gt=0),
    user: models.User = Depends(require("CREATE")),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200),
):
    data = file.file.read()
    context = get_request_context(request)
    with bound_request_context(context):
        if idempotency_key:
            return document_service.ingest_document_idempotent(
                db,
                user,
                idempotency_key=idempotency_key,
                filename=file.filename or "upload",
                data=data,
                content_type=file.content_type or "",
                folder_id=folder_id,
                context=context,
            )
        return document_service.ingest_document(
            db,
            user,
            filename=file.filename or "upload",
            data=data,
            content_type=file.content_type or "",
            folder_id=folder_id,
            context=context,
        )


@router.post(
    "/{doc_id}/versions",
    response_model=schemas.UploadResult,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_document_version(
    doc_id: Annotated[int, Path(ge=1)],
    request: Request,
    file: UploadFile = File(...),
    user: models.User = Depends(require("CREATE")),
    db: Session = Depends(get_db),
):
    data = file.file.read()
    context = get_request_context(request)
    with bound_request_context(context):
        return document_service.ingest_document_version(
            db, user, doc_id, filename=file.filename or "upload",
            data=data, content_type=file.content_type or "", context=context,
        )


@router.get("/{doc_id}/versions", response_model=list[schemas.VersionOut])
def list_document_versions(
    doc_id: Annotated[int, Path(ge=1)],
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    return document_service.list_versions(db, user, doc_id)


@router.get("/{doc_id}/versions/{version_id}/content")
def download_document_version(
    doc_id: Annotated[int, Path(ge=1)],
    version_id: Annotated[int, Path(ge=1)],
    request: Request,
    user: models.User = Depends(require("DOWNLOAD")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        download = document_service.get_version_download(
            db, user, doc_id, version_id, context=context
        )
    return FileResponse(download.path, media_type=download.content_type, filename=download.filename)


@router.post("/import-folder", response_model=schemas.ImportResult)
def import_folder(
    payload: schemas.ImportRequest,
    request: Request,
    user: models.User = Depends(
        require_any_global("ADMIN", IMPORT_SERVER_FOLDER_PERMISSION)
    ),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        return document_service.import_folder(
            db,
            user,
            payload,
            context=context,
        )


@router.get("", response_model=list[schemas.DocumentSummary])
def list_documents(
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
):
    return document_service.list_documents(
        db,
        allowed_ids=set(search_authorization.resolve_view_document_ids(db, user)),
        limit=limit,
    )


@router.get("/cursor", response_model=schemas.DocumentPage)
def list_documents_cursor(
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    try:
        after_id = _decode_cursor(cursor)
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    items, next_id = document_service.list_documents_page(
        db,
        allowed_ids=set(search_authorization.resolve_view_document_ids(db, user)),
        after_id=after_id,
        limit=limit,
    )
    return schemas.DocumentPage(
        items=items,
        next_cursor=_encode_cursor(next_id) if next_id else None,
    )


@router.get("/trash", response_model=list[schemas.DocumentSummary])
def list_trash_documents(
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
):
    return document_service.list_trash_documents(db, limit=limit)


@router.delete("/trash/empty", status_code=status.HTTP_204_NO_CONTENT)
def empty_trash(
    user: models.User = Depends(require("DELETE")),
    db: Session = Depends(get_db),
):
    document_service.empty_trash(db, user)
    return None


@router.get("/{doc_id}", response_model=schemas.DocumentDetail)
def get_document(
    doc_id: Annotated[int, Path(ge=1)],
    request: Request,
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        return document_service.get_document_detail(
            db,
            user,
            doc_id,
            context=context,
        )


@router.get("/{doc_id}/content")
def download_document(
    doc_id: Annotated[int, Path(ge=1)],
    request: Request,
    user: models.User = Depends(require("DOWNLOAD")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        download = document_service.get_download(
            db,
            user,
            doc_id,
            context=context,
        )
    return FileResponse(
        download.path,
        media_type=download.content_type,
        filename=download.filename,
    )


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    doc_id: Annotated[int, Path(ge=1)],
    request: Request,
    user: models.User = Depends(require("DELETE")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        document_service.delete_document(
            db,
            user,
            doc_id,
            context=context,
        )
    return None


@router.post("/{doc_id}/restore", response_model=schemas.DocumentSummary)
def restore_document(
    doc_id: Annotated[int, Path(ge=1)],
    request: Request,
    user: models.User = Depends(require("DELETE")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        return document_service.restore_document(db, user, doc_id, context=context)


@router.delete("/{doc_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
def purge_document(
    doc_id: Annotated[int, Path(ge=1)],
    request: Request,
    user: models.User = Depends(require("DELETE")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        document_service.purge_tombstoned_document(db, user, doc_id, context=context)
    return None


@router.post("/{doc_id}/move", response_model=schemas.DocumentSummary)
def move_document(
    doc_id: Annotated[int, Path(ge=1)],
    payload: schemas.MoveDocument,
    request: Request,
    user: models.User = Depends(require("MOVE")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        return document_service.move_document(
            db, user, doc_id, payload.folder_id, expected_updated_at=payload.expected_updated_at, context=context
        )


@router.patch("/{doc_id}/metadata", response_model=schemas.DocumentDetail)
def update_document_metadata(
    doc_id: Annotated[int, Path(ge=1)],
    payload: schemas.DocumentMetadataUpdate,
    request: Request,
    user: models.User = Depends(require("EDIT_METADATA")),
    db: Session = Depends(get_db),
):
    context = get_request_context(request)
    with bound_request_context(context):
        return document_service.update_metadata(db, user, doc_id, payload, context=context)
