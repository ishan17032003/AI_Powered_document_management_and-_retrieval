"""Authentication use cases."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Final, NoReturn

from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..observability import emit_event
from ..repositories import user_repository
from ..utils.request_context import RequestContext
from ..utils.security import (
    create_access_token,
    hash_password,
    password_hash_is_usable,
    verify_password,
)
from . import audit_service, rbac_service
from .exceptions import AuthenticationError
from .login_rate_limiter import LoginRateLimiter

MAX_LOGIN_USERNAME_LENGTH: Final = 80
MAX_LOGIN_PASSWORD_LENGTH: Final = 1024
_GENERIC_LOGIN_ERROR: Final = "Incorrect username or password"

# Each application process pays one normal PBKDF cost at import and retains only
# the resulting hash. Missing and ineligible accounts use it so they cannot
# skip password verification or expose a username-existence timing branch.
_DUMMY_PASSWORD_HASH: Final = hash_password(secrets.token_urlsafe(48))

_login_limiter = LoginRateLimiter(
    account_failure_limit=settings.login_account_failure_limit,
    source_failure_limit=settings.login_source_failure_limit,
    window_seconds=settings.login_failure_window_seconds,
    block_seconds=settings.login_block_seconds,
    max_entries=settings.login_rate_limit_max_entries,
    enabled=settings.login_rate_limit_enabled,
    secret_key=settings.secret_key,
)


def _credentials_are_bounded(username: str, password: str) -> bool:
    return (
        1 <= len(username) <= MAX_LOGIN_USERNAME_LENGTH
        and 1 <= len(password) <= MAX_LOGIN_PASSWORD_LENGTH
    )


def _reject_login(
    db: Session,
    *,
    user: models.User | None,
    context: RequestContext | None,
) -> NoReturn:
    audit_service.record(
        db,
        actor=user,
        action="LOGIN_FAILED",
        object_type="user",
        # Never pass a submitted credential into the audit boundary.
        object_id=user.id if user is not None else "",
        context=context,
    )
    raise AuthenticationError(_GENERIC_LOGIN_ERROR)


def _source_key(context: RequestContext | None) -> str:
    """Use only the already-sanitized request-context source address."""

    return context.ip if context is not None else ""


def _record_login_failure(
    *,
    username: str,
    context: RequestContext | None,
) -> None:
    decision = _login_limiter.record_failure(username, _source_key(context))
    if decision.dimension:
        # The event schema deliberately has no account/source fields. A
        # deployment can count this stable event as a throttle metric without
        # persisting usernames, IP addresses, or credential material.
        emit_event(
            "auth.login.throttled",
            context=context,
            component="auth",
            operation="login",
            outcome="blocked",
            count=decision.failure_count,
        )


def login(
    db: Session,
    *,
    username: str,
    password: str,
    context: RequestContext | None = None,
) -> schemas.Token:
    # Check before lookup so a blocked account/source cannot consume database
    # or password-verification capacity. All denials still use the same 401
    # detail and audit path, regardless of account existence.
    if not _login_limiter.acquire(username, _source_key(context)).allowed:
        _reject_login(db, user=None, context=context)
    try:
        if not _credentials_are_bounded(username, password):
            _record_login_failure(username=username, context=context)
            _reject_login(db, user=None, context=context)

        user = user_repository.get_by_username(db, username)
        eligible_user: models.User | None = None
        if (
            user is not None
            and user.status == "active"
            and (not settings.mfa_required or user.mfa_enabled)
            and password_hash_is_usable(user.password_hash)
        ):
            eligible_user = user
            password_hash = user.password_hash
        else:
            password_hash = _DUMMY_PASSWORD_HASH
        verified = verify_password(password, password_hash)
        if not verified:
            _record_login_failure(username=username, context=context)
            _reject_login(db, user=user, context=context)
        if eligible_user is None:
            _record_login_failure(username=username, context=context)
            _reject_login(db, user=user, context=context)

        _login_limiter.reset_account(username)

        audit_service.record(
            db,
            actor=eligible_user,
            action="LOGIN",
            object_type="user",
            object_id=eligible_user.id,
            context=context,
        )
        session_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        db.add(
            models.AuthSession(
                id=session_id,
                user_id=eligible_user.id,
                issued_at=now,
                expires_at=now + timedelta(minutes=settings.access_token_minutes),
                token_version=1,
            )
        )
        try:
            access_token = create_access_token(
                eligible_user.username,
                {"sid": session_id, "ver": 1},
            )
        except TypeError as exc:
            # A narrow compatibility path keeps legacy test doubles and extension
            # hooks (which accepted only ``subject``) working during rollout. The
            # real issuer always receives the durable session claims above.
            if "positional" not in str(exc) and "argument" not in str(exc):
                raise
            access_token = create_access_token(eligible_user.username)
        return schemas.Token(access_token=access_token)
    finally:
        _login_limiter.release(username, _source_key(context))


def current_user(db: Session, user: models.User) -> schemas.UserOut:
    return schemas.UserOut(
        id=user.id,
        username=user.username,
        name=user.name,
        email=user.email,
        status=user.status,
        roles=rbac_service.user_roles(db, user),
        permissions=sorted(rbac_service.user_permissions(db, user)),
    )
