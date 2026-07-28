"""JWT claim and durable session admission checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import jwt
import pytest

from app import deps
from app.config import settings
from app.utils.security import create_access_token, decode_token


def test_access_token_contains_bound_claims() -> None:
    payload = decode_token(create_access_token("alice"))
    assert payload["sub"] == "alice"
    assert payload["iss"] == settings.jwt_issuer
    assert payload["aud"] == settings.jwt_audience
    assert isinstance(payload["jti"], str) and len(payload["jti"]) == 32
    assert payload["exp"] - payload["iat"] <= settings.access_token_minutes * 60


def test_wrong_issuer_or_audience_is_rejected() -> None:
    token = create_access_token("alice")
    for claim, value in (("iss", "other"), ("aud", "other")):
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm], options={"verify_signature": False})
        payload[claim] = value
        forged = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
        with pytest.raises(jwt.InvalidTokenError):
            decode_token(forged)


def test_revoked_jti_is_rejected_before_user_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    token = create_access_token("alice")
    payload = decode_token(token)
    class DB:
        def scalar(self, *_args: object, **_kwargs: object) -> str:
            return payload["jti"]
    monkeypatch.setattr(deps.user_repository, "get_by_username", lambda *_a, **_k: None)
    with pytest.raises(Exception) as caught:
        deps.get_current_user(cast(Any, SimpleNamespace()), token, cast(Any, DB()))
    assert getattr(caught.value, "status_code", None) == 401


def test_revoked_session_or_token_version_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token = create_access_token("alice", {"sid": "s1", "ver": 2})
    class DB:
        def scalar(self, *_args: object, **_kwargs: object) -> None:
            return None
        def get(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                revoked_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                token_version=1,
            )
    monkeypatch.setattr(deps.user_repository, "get_by_username", lambda *_a, **_k: None)
    with pytest.raises(Exception) as caught:
        deps.get_current_user(cast(Any, SimpleNamespace()), token, cast(Any, DB()))
    assert getattr(caught.value, "status_code", None) == 401


def test_sqlite_naive_session_expiry_is_treated_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = create_access_token("alice", {"sid": "s1", "ver": 1})

    class DB:
        def scalar(self, *_args: object, **_kwargs: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                revoked_at=None,
                # SQLite DateTime returns this without tzinfo.
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(minutes=5),
                token_version=1,
            )

    monkeypatch.setattr(
        deps.user_repository,
        "get_by_username",
        lambda *_a, **_k: SimpleNamespace(id=1, username="alice", status="active"),
    )
    monkeypatch.setattr(deps, "bind_actor_to_request", lambda *_a, **_k: None)
    result = deps.get_current_user(
        cast(Any, SimpleNamespace(state=SimpleNamespace())),
        token,
        cast(Any, DB()),
    )
    assert result.username == "alice"
