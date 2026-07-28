"""Password hashing and JWT issue/verify.

Uses passlib's pbkdf2_sha256 (pure-Python, no native build) so it installs
cleanly on bleeding-edge Python without a bcrypt wheel.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from ..config import settings

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def password_hash_is_usable(hashed: object) -> bool:
    """Validate a stored hash structurally without doing password work."""

    if not isinstance(hashed, str) or not 1 <= len(hashed) <= 255:
        return False
    try:
        if pwd_context.identify(hashed) != "pbkdf2_sha256":
            return False
        # ``needs_update`` parses the complete hash but does not derive a key.
        # It catches recognized-yet-malformed values before the timed verify.
        pwd_context.needs_update(hashed)
    except (TypeError, ValueError):
        return False
    return True


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except (TypeError, ValueError):
        # A corrupted or legacy database value is an authentication failure,
        # never an application error or a reason to expose hash details.
        return False


def create_access_token(subject: str, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        "jti": uuid.uuid4().hex,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.algorithm],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        options={"require": ["sub", "iat", "exp", "jti", "iss", "aud"]},
    )
