"""Audited administrator-controlled identity lifecycle operations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..repositories import user_repository
from ..utils.security import hash_password
from ..utils.request_context import RequestContext
from . import audit_service, bootstrap_catalog_service, rbac_service
from .provisioning_service import (
    InitialAdministrator,
    ProvisioningError,
    validate_initial_administrator,
)


class IdentityLifecycleError(ValueError):
    pass


def _validate(candidate: InitialAdministrator) -> InitialAdministrator:
    try:
        return validate_initial_administrator(candidate)
    except ProvisioningError as exc:
        raise IdentityLifecycleError(exc.code) from exc


def _user(db: Session, user_id: int) -> models.User:
    found = db.get(models.User, user_id)
    if found is None:
        raise IdentityLifecycleError("USER_NOT_FOUND")
    return found


def _role(db: Session, name: str) -> models.Role:
    role = db.query(models.Role).filter(models.Role.name == name.strip()).first()
    if role is None:
        raise IdentityLifecycleError("ROLE_NOT_FOUND")
    return role


def _audit(db: Session, actor: models.User, target: models.User, action: str, context: RequestContext | None) -> None:
    audit_service.record(
        db,
        actor=actor,
        action=action,
        object_type="user",
        object_id=target.id,
        context=context,
    )


def create_user(
    db: Session,
    actor: models.User,
    payload: schemas.AdminUserCreate,
    *,
    context: RequestContext | None = None,
) -> models.User:
    validated = _validate(
        InitialAdministrator(payload.username, payload.name, payload.email, payload.password)
    )
    if user_repository.get_by_username(db, validated.username) is not None or db.query(models.User).filter(models.User.email == validated.email).first() is not None:
        raise IdentityLifecycleError("USER_ALREADY_EXISTS")
    role = _role(db, payload.role)
    target = models.User(
        username=validated.username,
        name=validated.name,
        email=validated.email,
        password_hash=hash_password(validated.password),
        status="active",
        mfa_enabled=False,
    )
    db.add(target)
    db.flush()
    db.add(models.Assignment(user_id=target.id, role_id=role.id, scope_type="GLOBAL", effect="ALLOW"))
    
    if role.name == "Super Admin":
        permissions = db.query(models.Permission).all()
        for p in permissions:
            db.add(
                models.AccessRule(
                    principal_type="USER",
                    user_id=target.id,
                    group_id=None,
                    permission_id=p.id,
                    scope_type="GLOBAL",
                    scope_id=None,
                    effect="ALLOW",
                    inherits=True,
                    is_active=True,
                    expires_at=None,
                    reason="Global grant for Super Admin",
                    created_by=actor.id,
                )
            )

    # Sync group membership for the assigned role
    group = db.query(models.Group).filter(models.Group.name == role.name).first()
    if group:
        db.add(models.GroupMembership(
            group_id=group.id,
            user_id=target.id,
            created_by=actor.id
        ))

    _audit(db, actor, target, "ADMIN_USER_CREATED", context)
    db.commit()
    db.refresh(target)
    return target


def set_status(db: Session, actor: models.User, user_id: int, status: str, *, context: RequestContext | None = None) -> models.User:
    target = _user(db, user_id)
    if target.id == actor.id and status == "suspended":
        raise IdentityLifecycleError("SELF_SUSPENSION_NOT_ALLOWED")
    target.status = status
    if status == "suspended":
        revoke_sessions(db, actor, target, reason="user_suspended", context=context, commit=False)
    _audit(db, actor, target, "ADMIN_USER_SUSPENDED" if status == "suspended" else "ADMIN_USER_REACTIVATED", context)
    db.commit()
    return target


def assign_role(db: Session, actor: models.User, user_id: int, role_name: str, *, context: RequestContext | None = None) -> models.User:
    target = _user(db, user_id)
    role = _role(db, role_name)
    existing = db.query(models.Assignment).filter(models.Assignment.user_id == target.id, models.Assignment.role_id == role.id, models.Assignment.scope_type == "GLOBAL", models.Assignment.scope_id.is_(None), models.Assignment.effect == "ALLOW").first()
    if existing is None:
        db.add(models.Assignment(user_id=target.id, role_id=role.id, scope_type="GLOBAL", effect="ALLOW"))
        
        if role.name == "Super Admin":
            permissions = db.query(models.Permission).all()
            for p in permissions:
                # Check if a rule already exists
                rule_exists = db.query(models.AccessRule).filter(
                    models.AccessRule.principal_type == "USER",
                    models.AccessRule.user_id == target.id,
                    models.AccessRule.permission_id == p.id,
                    models.AccessRule.scope_type == "GLOBAL",
                    models.AccessRule.scope_id.is_(None),
                    models.AccessRule.effect == "ALLOW"
                ).first()
                if not rule_exists:
                    db.add(
                        models.AccessRule(
                            principal_type="USER",
                            user_id=target.id,
                            group_id=None,
                            permission_id=p.id,
                            scope_type="GLOBAL",
                            scope_id=None,
                            effect="ALLOW",
                            inherits=True,
                            is_active=True,
                            expires_at=None,
                            reason="Global grant for Super Admin",
                            created_by=actor.id,
                        )
                    )

        # Sync group membership for the assigned role
        group = db.query(models.Group).filter(models.Group.name == role.name).first()
        if group:
            membership = db.query(models.GroupMembership).filter(
                models.GroupMembership.group_id == group.id,
                models.GroupMembership.user_id == target.id
            ).first()
            if not membership:
                db.add(models.GroupMembership(
                    group_id=group.id,
                    user_id=target.id,
                    created_by=actor.id
                ))

    _audit(db, actor, target, "ADMIN_ROLE_ASSIGNED", context)
    db.commit()
    return target


def reset_password(db: Session, actor: models.User, user_id: int, password: str, *, context: RequestContext | None = None) -> models.User:
    target = _user(db, user_id)
    validated = _validate(InitialAdministrator(target.username, target.name, target.email, password))
    target.password_hash = hash_password(validated.password)
    revoke_sessions(db, actor, target, reason="credential_reset", context=context, commit=False)
    _audit(db, actor, target, "ADMIN_CREDENTIAL_RESET", context)
    db.commit()
    return target


def revoke_sessions(db: Session, actor: models.User, target: models.User, *, reason: str, context: RequestContext | None = None, commit: bool = True) -> int:
    now = datetime.now(UTC)
    sessions = db.scalars(select(models.AuthSession).where(models.AuthSession.user_id == target.id, models.AuthSession.revoked_at.is_(None))).all()
    for session in sessions:
        session.revoked_at = now
    _audit(db, actor, target, "ADMIN_SESSIONS_REVOKED", context)
    if commit:
        db.commit()
    return len(sessions)
