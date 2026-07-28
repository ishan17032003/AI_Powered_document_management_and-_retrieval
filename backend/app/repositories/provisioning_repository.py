"""Transaction-neutral persistence for one-time administrator provisioning."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models


def acquire_database_lock(db: Session, lock_key: int) -> bool:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
        return True
    if dialect == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
        return True
    return False


def has_any_user(db: Session) -> bool:
    return db.query(models.User.id).limit(1).first() is not None


def add_user(
    db: Session,
    *,
    username: str,
    name: str,
    email: str,
    password_hash: str,
) -> models.User:
    user = models.User(
        username=username,
        name=name,
        email=email,
        password_hash=password_hash,
        status="active",
        mfa_enabled=False,
    )
    db.add(user)
    db.flush()
    return user


def add_global_assignment(
    db: Session,
    *,
    user_id: int,
    role_id: int,
) -> None:
    db.add(
        models.Assignment(
            user_id=user_id,
            role_id=role_id,
            scope_type="GLOBAL",
            scope_id=None,
            effect="ALLOW",
        )
    )


def add_provisioning_audit(
    db: Session,
    *,
    user: models.User,
    details: str,
) -> None:
    db.add(
        models.AuditLog(
            actor_id=user.id,
            actor_name=f"user:{user.id}",
            action="INITIAL_ADMIN_PROVISIONED",
            object_type="user",
            object_id=str(user.id),
            details=details,
        )
    )
