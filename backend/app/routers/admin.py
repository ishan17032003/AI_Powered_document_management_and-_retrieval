"""HTTP routes for administration and dashboard statistics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require, require_global
from ..repositories import job_repository
from ..services import (
    admin_identity_service,
    admin_service,
    ingestion_worker,
    reconciliation_monitor,
    search_authorization,
)
from ..utils.cursors import cursor_int, cursor_time, decode_cursor, encode_cursor
from ..utils.request_context import get_request_context

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _job_out(job: models.IngestionJob) -> schemas.JobOut:
    import json

    try:
        results = json.loads(job.stage_results or "{}")
        if not isinstance(results, dict):
            results = {}
    except (TypeError, ValueError):
        results = {}
    try:
        degraded = json.loads(job.degraded_stages or "[]")
        if not isinstance(degraded, list):
            degraded = []
    except (TypeError, ValueError):
        degraded = []
    return schemas.JobOut(
        id=job.id, state=job.state, stage_version=job.stage_version,
        idempotency_key=job.idempotency_key, attempt_count=job.attempt_count,
        created_at=job.created_at, updated_at=job.updated_at,
        completed_at=job.completed_at, document_id=job.document_id,
        version_id=job.version_id, stage=job.stage,
        stage_results=results, degraded_stages=degraded,
    )


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=schemas.JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def reprocess_document(
    document_id: int = Path(gt=0),
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    try:
        job = ingestion_worker.enqueue_reprocessing(
            db, document_id=document_id, requested_by=actor.id
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="Document unavailable") from exc
    return _job_out(job)


@router.get("/users", response_model=list[schemas.UserAdminOut])
def list_users(
    user: models.User = Depends(require("ADMIN")),
    db: Session = Depends(get_db),
):
    return admin_service.list_users(db)


@router.get("/users/cursor", response_model=schemas.UserPage)
def list_users_cursor(
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=500),
    user: models.User = Depends(require("ADMIN")),
    db: Session = Depends(get_db),
):
    try:
        values = decode_cursor(cursor)
        after_id = cursor_int(values, "id") if values else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    items, next_id = admin_service.list_users_page(db, after_id=after_id, limit=limit)
    return schemas.UserPage(items=items, next_cursor=encode_cursor(id=next_id) if next_id else None)


@router.get("/jobs/cursor", response_model=schemas.JobPage)
def list_jobs_cursor(
    cursor: str | None = Query(default=None, max_length=256),
    state: str | None = Query(default=None, min_length=1, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    user: models.User = Depends(require("ADMIN")),
    db: Session = Depends(get_db),
):
    try:
        values = decode_cursor(cursor)
        after_id = values.get("id") if values else None
        if after_id is not None and (not isinstance(after_id, str) or not after_id):
            raise ValueError("invalid cursor")
        after_created = cursor_time(values, "created_at") if values else None
        jobs = job_repository.list_ingestion_jobs_after(db, state=state, after_created_at=after_created, after_id=after_id, limit=limit + 1)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor or job filter") from exc
    has_next = len(jobs) > limit
    items = [_job_out(job) for job in jobs[:limit]]
    next_cursor = None
    if has_next and items:
        last = items[-1]
        next_cursor = encode_cursor(created_at=last.created_at.isoformat(), id=last.id)
    return schemas.JobPage(items=items, next_cursor=next_cursor)

@router.post("/users", response_model=schemas.UserAdminOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: schemas.AdminUserCreate, request: Request, actor: models.User = Depends(require_global("ADMIN")), db: Session = Depends(get_db)):
    try:
        user = admin_identity_service.create_user(db, actor, payload, context=get_request_context(request))
        return schemas.UserAdminOut(id=user.id, username=user.username, name=user.name, email=user.email, status=user.status, roles=admin_service.rbac_service.user_roles(db, user))
    except admin_identity_service.IdentityLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/users/{user_id}", response_model=schemas.UserAdminOut)
def update_user(payload: schemas.AdminUserPatch, request: Request, user_id: int = Path(gt=0), actor: models.User = Depends(require_global("ADMIN")), db: Session = Depends(get_db)):
    if payload.status is None:
        raise HTTPException(status_code=400, detail="No lifecycle change supplied")
    try:
        user = admin_identity_service.set_status(db, actor, user_id, payload.status, context=get_request_context(request))
        return schemas.UserAdminOut(id=user.id, username=user.username, name=user.name, email=user.email, status=user.status, roles=admin_service.rbac_service.user_roles(db, user))
    except admin_identity_service.IdentityLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{user_id}/role", response_model=schemas.UserAdminOut)
def assign_role(payload: schemas.AdminRoleAssignment, request: Request, user_id: int = Path(gt=0), actor: models.User = Depends(require_global("ADMIN")), db: Session = Depends(get_db)):
    try:
        user = admin_identity_service.assign_role(db, actor, user_id, payload.role, context=get_request_context(request))
        return schemas.UserAdminOut(id=user.id, username=user.username, name=user.name, email=user.email, status=user.status, roles=admin_service.rbac_service.user_roles(db, user))
    except admin_identity_service.IdentityLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{user_id}/reset-credential", response_model=schemas.UserAdminOut)
def reset_credential(payload: schemas.AdminCredentialReset, request: Request, user_id: int = Path(gt=0), actor: models.User = Depends(require_global("ADMIN")), db: Session = Depends(get_db)):
    try:
        user = admin_identity_service.reset_password(db, actor, user_id, payload.password, context=get_request_context(request))
        return schemas.UserAdminOut(id=user.id, username=user.username, name=user.name, email=user.email, status=user.status, roles=admin_service.rbac_service.user_roles(db, user))
    except admin_identity_service.IdentityLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{user_id}/revoke-sessions", status_code=status.HTTP_204_NO_CONTENT)
def revoke_user_sessions(request: Request, user_id: int = Path(gt=0), actor: models.User = Depends(require_global("ADMIN")), db: Session = Depends(get_db)):
    try:
        admin_identity_service.revoke_sessions(db, actor, admin_identity_service._user(db, user_id), reason="admin_requested", context=get_request_context(request))
    except admin_identity_service.IdentityLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rbac-matrix")
def rbac_matrix(
    user: models.User = Depends(require("ADMIN")),
):
    return admin_service.rbac_matrix()


@router.get("/stats")
def dashboard_stats(
    user: models.User = Depends(require("VIEW")),
    db: Session = Depends(get_db),
):
    return admin_service.dashboard_stats(
        db,
        visible_document_ids=search_authorization.resolve_view_document_ids(db, user),
    )


@router.get("/reconciliation/metrics")
def reconciliation_metrics(
    stale_after_seconds: int = Query(default=3600, ge=1, le=604800),
    user: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    """Read-only drift and stale-processing metrics for operators/alerting."""
    return reconciliation_monitor.collect_metrics(
        db, stale_after_seconds=stale_after_seconds
    )


@router.get("/release-metrics")
def release_metrics(
    stale_after_seconds: int = Query(default=3600, ge=1, le=604800),
    user: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    """Read-only release-critical queue, drift, and optional health metrics."""
    return reconciliation_monitor.collect_release_metrics(
        db, stale_after_seconds=stale_after_seconds
    )


@router.get("/doc-classes", response_model=list[schemas.DocClassOut])
def list_doc_classes(
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    """Return all document taxonomy classes sorted by name.

    Used by the Super Admin class-selector dropdown in the Repository view.
    """
    from sqlalchemy import select as _select, asc as _asc

    rows = db.scalars(
        _select(models.DocClass).order_by(_asc(models.DocClass.name))
    ).all()
    return [schemas.DocClassOut.model_validate(row) for row in rows]


@router.patch(
    "/documents/{doc_id}/class",
    response_model=schemas.DocumentSummary,
)
def reclassify_document(
    doc_id: int = Path(gt=0),
    payload: schemas.ReclassifyDocument = ...,
    request: Request = ...,
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    """Manually reassign the document class (Super Admin only).

    Accepts either an existing ``class_id`` or a free-text ``class_name``
    (which is created on the fly if it does not yet exist).  When the MinIO
    storage backend is active the binary is moved to the new class bucket.
    """
    from ..services import document_service
    from ..services.exceptions import NotFoundError, PermissionDeniedError
    from ..utils.request_context import get_request_context
    from ..repositories import document_repository

    # Resolve class_name → class_id when only a name is provided.
    resolved_class_id = payload.class_id
    if resolved_class_id is None and payload.class_name:
        cls = document_repository.get_or_create_class(db, payload.class_name)
        db.flush()
        resolved_class_id = cls.id

    try:
        return document_service.reclassify_document(
            db,
            actor,
            doc_id,
            resolved_class_id,
            context=get_request_context(request),
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

