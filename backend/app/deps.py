"""FastAPI dependencies at the HTTP/application boundary."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .repositories import user_repository
from .services import rbac_service
from .utils.request_context import bind_actor_to_request
from .utils.security import decode_token


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive DateTime values before token comparisons."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        username = payload.get("sub")
        if not username:
            raise credentials_error
        jti = payload.get("jti")
        sid = payload.get("sid")
        token_version = payload.get("ver")
        now = datetime.now(timezone.utc)
        if jti:
            try:
                revoked = db.scalar(
                    select(models.AuthTokenRevocation.jti).where(
                        models.AuthTokenRevocation.jti == jti,
                        models.AuthTokenRevocation.expires_at > now,
                    )
                )
            except (OperationalError, ProgrammingError):
                revoked = None
            if revoked:
                raise credentials_error
        if sid:
            try:
                session = db.get(models.AuthSession, sid)
            except (OperationalError, ProgrammingError) as exc:
                raise credentials_error from exc
            if (
                session is None
                or session.revoked_at is not None
                or _as_utc(session.expires_at) <= now
                or session.token_version != token_version
            ):
                raise credentials_error
    except HTTPException:
        raise
    except Exception as exc:
        raise credentials_error from exc

    user = user_repository.get_by_username(db, username)
    if user is None or user.status != "active":
        raise credentials_error
    bind_actor_to_request(request, user.id)
    return user


def require(permission: str):
    """Build a dependency that enforces one global permission."""

    def dependency(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if not rbac_service.has_permission(db, user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
            )
        return user

    return dependency


def require_global(permission: str):
    """Build a dependency for an application-wide capability.

    Resource-scoped role assignments cannot authorize shared-state mutation.
    """

    def dependency(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if not rbac_service.has_global_permission(db, user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
            )
        return user

    return dependency


def require_any_global(*permissions: str):
    """Require one of several explicit global capabilities."""

    def dependency(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if not any(rbac_service.has_global_permission(db, user, item) for item in permissions):
            joined = ", ".join(permissions)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing one of required permissions: {joined}",
            )
        return user

    return dependency


def user_roles(db: Session, user: models.User) -> list[str]:
    return rbac_service.user_roles(db, user)


def viewable_document_ids(
    db: Session,
    user: models.User,
) -> set[int] | None:
    return rbac_service.viewable_document_ids(db, user)
