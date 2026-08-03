"""Resource access-management APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_global
from ..domain.resources import PrincipalKind, ResourceRef, ResourceScope
from ..repositories import access_rule_repository
from ..services import access_rule_service, effective_access_service
from ..utils.request_context import get_request_context

router = APIRouter(prefix="/api/v1/access", tags=["access"])


def _out(rule: models.AccessRule) -> schemas.AccessRuleOut:
    return schemas.AccessRuleOut(
        id=rule.id,
        principal_type=rule.principal_type,
        principal_id=rule.user_id or rule.group_id,  # exactly one is enforced by MIG-003
        permission=rule.permission.code,
        scope_type=rule.scope_type,
        scope_id=rule.scope_id,
        effect=rule.effect,
        inherits=rule.inherits,
        is_active=rule.is_active,
        expires_at=rule.expires_at,
        reason=rule.reason,
        created_by=rule.created_by,
        created_at=rule.created_at,
    )


def _scope(value: str) -> ResourceScope:
    try:
        return ResourceScope(value.upper())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid access scope") from exc


@router.get("/effective", response_model=schemas.EffectiveAccessOut)
def explain_effective_access(
    user_id: Annotated[int, Query(gt=0)],
    permission: str = "VIEW",
    scope: str = "DOC",
    scope_id: int | None = None,
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
):
    del actor
    try:
        selected_scope = _scope(scope)
        if selected_scope is ResourceScope.GLOBAL:
            ref = ResourceRef.global_scope()
        elif scope_id is not None:
            ref = ResourceRef(selected_scope, scope_id)
        else:
            raise ValueError("scope_id is required")
        return effective_access_service.explain(db, target_user_id=user_id, permission=permission, resource=ref)
    except (ValueError, LookupError, access_rule_repository.AuthorizationInputUnavailable) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _mutate(
    db: Session,
    actor: models.User,
    payload: schemas.AccessRuleCreate,
    scope: ResourceScope,
    scope_id: int | None,
    request: Request,
) -> schemas.AccessRuleOut:
    try:
        rule = access_rule_service.upsert_rule(
            db,
            actor=actor,
            principal_type=PrincipalKind(payload.principal_type),
            principal_id=payload.principal_id,
            permission_code=payload.permission,
            scope_type=scope,
            scope_id=scope_id,
            effect=payload.effect,
            inherits=payload.inherits,
            expires_at=payload.expires_at,
            reason=payload.reason,
            context=get_request_context(request),
        )
        db.commit()
        db.refresh(rule)
        return _out(rule)
    except (ValueError, PermissionError, LookupError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _list(scope: ResourceScope, scope_id: int, actor: models.User, db: Session) -> list[schemas.AccessRuleOut]:
    try:
        return [_out(rule) for rule in access_rule_service.list_rules(db, scope_type=scope, scope_id=scope_id)]
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{scope}/{scope_id}/rules", response_model=list[schemas.AccessRuleOut])
def list_access_rules(scope: str, scope_id: Annotated[int, Path(gt=0)], actor: models.User = Depends(require_global("MANAGE_PERMISSIONS")), db: Session = Depends(get_db)):
    return _list(_scope(scope), scope_id, actor, db)


@router.post("/{scope}/{scope_id}/rules", response_model=schemas.AccessRuleOut, status_code=status.HTTP_201_CREATED)
def grant_access_rule(scope: str, scope_id: Annotated[int, Path(gt=0)], payload: schemas.AccessRuleCreate, request: Request, actor: models.User = Depends(require_global("MANAGE_PERMISSIONS")), db: Session = Depends(get_db)):
    return _mutate(db, actor, payload, _scope(scope), scope_id, request)


@router.post("/{scope}/rules", response_model=schemas.AccessRuleOut, status_code=status.HTTP_201_CREATED)
def grant_global_access_rule(scope: str, payload: schemas.AccessRuleCreate, request: Request, actor: models.User = Depends(require_global("MANAGE_PERMISSIONS")), db: Session = Depends(get_db)):
    if _scope(scope) is not ResourceScope.GLOBAL:
        raise HTTPException(status_code=422, detail="Only GLOBAL scope omits scope_id")
    return _mutate(db, actor, payload, ResourceScope.GLOBAL, None, request)


@router.delete("/rules/{rule_id}", response_model=schemas.AccessRuleOut)
def revoke_access_rule(rule_id: Annotated[int, Path(gt=0)], request: Request, actor: models.User = Depends(require_global("MANAGE_PERMISSIONS")), db: Session = Depends(get_db)):
    try:
        rule = access_rule_service.deactivate_rule(db, actor=actor, rule_id=rule_id, context=get_request_context(request))
        db.commit()
        db.refresh(rule)
        return _out(rule)
    except (ValueError, PermissionError, LookupError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=404 if isinstance(exc, LookupError) else 400, detail=str(exc)) from exc


@router.patch("/rules/{rule_id}", response_model=schemas.AccessRuleOut)
def update_access_rule(
    rule_id: Annotated[int, Path(gt=0)],
    payload: schemas.AccessRuleCreate,
    request: Request,
    actor: models.User = Depends(require_global("MANAGE_PERMISSIONS")),
    db: Session = Depends(get_db),
):
    try:
        current = db.get(models.AccessRule, rule_id)
        if current is None:
            raise LookupError("access rule not found")
        rule = access_rule_service.update_rule(
            db,
            actor=actor,
            rule_id=rule_id,
            principal_type=PrincipalKind(payload.principal_type),
            principal_id=payload.principal_id,
            permission_code=payload.permission,
            scope_type=ResourceScope(current.scope_type),
            scope_id=current.scope_id,
            effect=payload.effect,
            inherits=payload.inherits,
            expires_at=payload.expires_at,
            reason=payload.reason,
            context=get_request_context(request),
        )
        db.commit()
        db.refresh(rule)
        return _out(rule)
    except (ValueError, PermissionError, LookupError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=404 if isinstance(exc, LookupError) else 400, detail=str(exc)) from exc


# ── User-centric document access view ─────────────────────────────────────────

@router.get(
    "/user/{user_id}/doc-rules",
    response_model=list[schemas.UserDocRuleOut],
    summary="List all DOC-scoped access rules for a user (incl. revoke history)",
)
def list_user_doc_rules(
    user_id: Annotated[int, Path(gt=0)],
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
) -> list[schemas.UserDocRuleOut]:
    """Return every DOC-scoped access_rule for the given user.

    Includes both active and revoked (is_active=False) rules so admins
    can see the full history.  All permissions (VIEW, DOWNLOAD, …) are
    returned.  Requires at least the ADMIN global permission.
    """
    from sqlalchemy import select as _select

    # Verify the target user exists.
    target = db.get(models.User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Join access_rules → documents to get document title + class.
    stmt = (
        _select(models.AccessRule, models.Document)
        .join(
            models.Document,
            (models.AccessRule.scope_id == models.Document.id)
            & (models.AccessRule.scope_type == "DOC"),
        )
        .where(
            models.AccessRule.user_id == user_id,
            models.AccessRule.scope_type == "DOC",
        )
        .order_by(models.AccessRule.created_at.desc())
        .limit(500)
    )

    rows = db.execute(stmt).all()

    result: list[schemas.UserDocRuleOut] = []
    for rule, doc in rows:
        doc_class_name: str | None = None
        if doc.doc_class is not None:
            doc_class_name = doc.doc_class.name
        result.append(
            schemas.UserDocRuleOut(
                rule_id=rule.id,
                doc_id=doc.id,
                doc_title=doc.title,
                doc_class=doc_class_name,
                effect=rule.effect,
                permission=rule.permission.code,
                reason=rule.reason,
                is_active=rule.is_active,
                created_at=rule.created_at,
            )
        )
    return result


# ── Access-control matrix (all users × all docs) ───────────────────────────────

@router.get(
    "/all-doc-rules",
    response_model=list[schemas.AllDocRuleOut],
    summary="List every DOC-scoped access rule across all users",
)
def list_all_doc_rules(
    actor: models.User = Depends(require_global("ADMIN")),
    db: Session = Depends(get_db),
) -> list[schemas.AllDocRuleOut]:
    """Return every DOC-scoped access_rule for every user, including history.

    Sorted by user name then document title.  Requires ADMIN permission.
    """
    from sqlalchemy import select as _select

    stmt = (
        _select(models.AccessRule, models.Document, models.User)
        .join(
            models.Document,
            (models.AccessRule.scope_id == models.Document.id)
            & (models.AccessRule.scope_type == "DOC"),
        )
        .join(
            models.User,
            models.AccessRule.user_id == models.User.id,
        )
        .where(
            models.AccessRule.scope_type == "DOC",
            models.AccessRule.user_id.is_not(None),
        )
        .order_by(models.User.name, models.Document.title, models.AccessRule.created_at.desc())
        .limit(1000)
    )

    rows = db.execute(stmt).all()

    result: list[schemas.AllDocRuleOut] = []
    for rule, doc, user in rows:
        doc_class_name: str | None = None
        if doc.doc_class is not None:
            doc_class_name = doc.doc_class.name
        result.append(
            schemas.AllDocRuleOut(
                rule_id=rule.id,
                user_id=user.id,
                user_name=user.name,
                user_username=user.username,
                doc_id=doc.id,
                doc_title=doc.title,
                doc_class=doc_class_name,
                effect=rule.effect,
                permission=rule.permission.code,
                reason=rule.reason,
                is_active=rule.is_active,
                created_at=rule.created_at,
            )
        )
    return result
