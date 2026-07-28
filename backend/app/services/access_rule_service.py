"""Transactional access-rule management boundary.

Callers own the transaction; this service validates delegation and all rule
inputs before mutating the ACL and policy revision.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..repositories import policy_revision_repository
from ..domain.resources import PrincipalKind, ResourceScope
from ..utils.request_context import RequestContext
from . import audit_service, rbac_service

MAX_REASON_LENGTH = 1000


def _require_id(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _validate_scope(db: Session, scope: ResourceScope, scope_id: int | None) -> None:
    if scope is ResourceScope.GLOBAL:
        if scope_id is not None:
            raise ValueError("global rules cannot have scope_id")
        return
    target = {ResourceScope.CABINET: models.Cabinet,
              ResourceScope.FOLDER: models.Folder,
              ResourceScope.DOC: models.Document}[scope]
    if scope_id is None or db.get(target, _require_id(scope_id, "scope_id")) is None:
        raise ValueError("scope resource does not exist")


def upsert_rule(
    db: Session,
    *,
    actor: models.User,
    principal_type: PrincipalKind,
    principal_id: int,
    permission_code: str,
    scope_type: ResourceScope,
    scope_id: int | None,
    effect: str,
    inherits: bool,
    expires_at: datetime | None = None,
    reason: str | None = None,
    context: RequestContext | None = None,
) -> models.AccessRule:
    """Create one ACL rule after checking management delegation.

    The operation is transaction-neutral: no commit or rollback is performed.
    """
    if not isinstance(principal_type, PrincipalKind) or not isinstance(scope_type, ResourceScope):
        raise ValueError("invalid principal or scope type")
    principal_id = _require_id(principal_id, "principal_id")
    if not rbac_service.has_global_permission(db, actor, "MANAGE_PERMISSIONS"):
        raise PermissionError("MANAGE_PERMISSIONS is required")
    if effect not in {"ALLOW", "DENY"} or type(inherits) is not bool:
        raise ValueError("invalid effect or inherits")
    if not isinstance(permission_code, str) or not permission_code.strip() or len(permission_code) > 40:
        raise ValueError("permission_code must be a bounded non-empty string")
    if reason is not None and (not reason.strip() or len(reason) > MAX_REASON_LENGTH):
        raise ValueError("reason must be empty or at most 1000 characters")
    if expires_at is not None and (expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc)):
        raise ValueError("expires_at must be a future timezone-aware datetime")
    _validate_scope(db, scope_type, scope_id)
    principal = models.User if principal_type is PrincipalKind.USER else models.Group
    if db.get(principal, principal_id) is None:
        raise ValueError("principal does not exist")
    permission = db.scalar(select(models.Permission).where(models.Permission.code == permission_code.strip()))
    if permission is None:
        raise ValueError("permission does not exist")
    rule = models.AccessRule(
        principal_type=principal_type.value,
        user_id=principal_id if principal_type is PrincipalKind.USER else None,
        group_id=principal_id if principal_type is PrincipalKind.GROUP else None,
        permission_id=permission.id,
        scope_type=scope_type.value,
        scope_id=scope_id,
        effect=effect,
        inherits=inherits,
        is_active=True,
        expires_at=expires_at.astimezone(timezone.utc).replace(tzinfo=None) if expires_at else None,
        reason=reason.strip() if reason else None,
        created_by=actor.id,
    )
    db.add(rule)
    policy_revision_repository.bump(db, actor_id=actor.id)
    db.flush()
    audit_service.record(
        db,
        actor=actor,
        action="ACCESS_RULE_GRANTED",
        object_type="access_rule",
        object_id=rule.id,
        context=context,
    )
    return rule


def deactivate_rule(
    db: Session,
    *,
    actor: models.User,
    rule_id: int,
    context: RequestContext | None = None,
) -> models.AccessRule:
    if not rbac_service.has_global_permission(db, actor, "MANAGE_PERMISSIONS"):
        raise PermissionError("MANAGE_PERMISSIONS is required")
    rule = db.get(models.AccessRule, _require_id(rule_id, "rule_id"))
    if rule is None:
        raise LookupError("access rule not found")
    if rule.is_active:
        rule.is_active = False
        policy_revision_repository.bump(db, actor_id=actor.id)
        db.flush()
        audit_service.record(
            db,
            actor=actor,
            action="ACCESS_RULE_REVOKED",
            object_type="access_rule",
            object_id=rule.id,
            context=context,
        )
    return rule


def update_rule(
    db: Session,
    *,
    actor: models.User,
    rule_id: int,
    principal_type: PrincipalKind,
    principal_id: int,
    permission_code: str,
    scope_type: ResourceScope,
    scope_id: int | None,
    effect: str,
    inherits: bool,
    expires_at: datetime | None = None,
    reason: str | None = None,
    context: RequestContext | None = None,
) -> models.AccessRule:
    """Replace a rule transactionally, preserving an auditable old revision."""
    deactivate_rule(db, actor=actor, rule_id=rule_id, context=context)
    return upsert_rule(
        db,
        actor=actor,
        principal_type=principal_type,
        principal_id=principal_id,
        permission_code=permission_code,
        scope_type=scope_type,
        scope_id=scope_id,
        effect=effect,
        inherits=inherits,
        expires_at=expires_at,
        reason=reason,
        context=context,
    )


def list_rules(
    db: Session,
    *,
    scope_type: ResourceScope,
    scope_id: int | None,
    limit: int = 100,
) -> list[models.AccessRule]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    _validate_scope(db, scope_type, scope_id)
    return (
        db.query(models.AccessRule)
        .filter(
            models.AccessRule.scope_type == scope_type.value,
            models.AccessRule.scope_id == scope_id,
        )
        .order_by(models.AccessRule.id.desc())
        .limit(limit)
        .all()
    )
