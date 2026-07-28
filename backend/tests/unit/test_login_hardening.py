"""Bounded login-body and credential-verification security checks."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import NoReturn, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Scope

from app.middleware import (
    LOGIN_PATH,
    MAX_LOGIN_BODY_BYTES,
    LoginBodyLimitMiddleware,
    RequestCorrelationMiddleware,
)
from app.observability import StructuredEventFormatter, event_logger


class _RenderedEventCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[str] = []
        self.setFormatter(StructuredEventFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.rendered.append(self.format(record))


@pytest.fixture
def rendered_events() -> Iterator[_RenderedEventCapture]:
    capture = _RenderedEventCapture()
    logger = event_logger()
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)


def test_oversized_declared_login_body_is_rejected_before_form_parsing(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import auth_service

    def fail_if_parsed(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("oversized login body reached the route")

    monkeypatch.setattr(auth_service, "login", fail_if_parsed)
    response = api_client.post(
        LOGIN_PATH,
        content=b"username=x&password=y",
        headers={
            "Content-Length": str(MAX_LOGIN_BODY_BYTES + 1),
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "http://localhost:5173",
        },
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == ("http://localhost:5173")
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Correlation-ID"]


def test_chunked_login_body_stops_forwarding_at_limit_and_keeps_correlation() -> None:
    forwarded: list[bytes] = []
    sent: list[Message] = []
    receive_calls = 0
    incoming: list[Message] = [
        {
            "type": "http.request",
            "body": b"a" * MAX_LOGIN_BODY_BYTES,
            "more_body": True,
        },
        {
            "type": "http.request",
            "body": b"credential-canary-over-limit",
            "more_body": True,
        },
        {
            "type": "http.request",
            "body": b"must-not-be-read",
            "more_body": False,
        },
    ]

    async def receive() -> Message:
        nonlocal receive_calls
        message = incoming[receive_calls]
        receive_calls += 1
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    async def consume_body(
        _scope: Scope,
        app_receive,
        _send,
    ) -> None:
        while True:
            message = await app_receive()
            forwarded.append(message.get("body", b""))
            if not message.get("more_body", False):
                return

    scope = cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": LOGIN_PATH,
            "raw_path": LOGIN_PATH.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"transfer-encoding", b"chunked")],
            "client": ("127.0.0.1", 41234),
            "server": ("docvault.test", 80),
        },
    )
    application: ASGIApp = RequestCorrelationMiddleware(
        LoginBodyLimitMiddleware(consume_body)
    )

    async def exercise() -> None:
        await application(scope, receive, send)

    asyncio.run(exercise())

    assert receive_calls == 2
    assert forwarded == [b"a" * MAX_LOGIN_BODY_BYTES]
    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 413
    headers = Headers(raw=response_start["headers"])
    assert headers["X-Request-ID"]
    assert headers["X-Correlation-ID"]
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert json.loads(response_body) == {"detail": "Request body too large"}
    assert b"credential-canary-over-limit" not in response_body


def test_body_limit_is_exactly_scoped_to_the_login_route() -> None:
    application_called = False
    sent: list[Message] = []

    async def receive() -> Message:
        return {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    async def send(message: Message) -> None:
        sent.append(message)

    async def endpoint(
        _scope: Scope,
        _receive,
        app_send,
    ) -> None:
        nonlocal application_called
        application_called = True
        await app_send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await app_send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )

    scope = cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/documents",
            "raw_path": b"/api/v1/documents",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (
                    b"content-length",
                    str(MAX_LOGIN_BODY_BYTES + 1).encode("ascii"),
                )
            ],
            "client": ("127.0.0.1", 41234),
            "server": ("docvault.test", 80),
        },
    )
    application: ASGIApp = LoginBodyLimitMiddleware(endpoint)

    async def exercise() -> None:
        await application(scope, receive, send)

    asyncio.run(exercise())

    assert application_called
    assert (
        next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]
        == 204
    )


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("u" * 81, "bounded-password"),
        ("bounded-user", "p" * 1025),
    ],
)
def test_oversized_credentials_do_not_reach_lookup_or_password_verification(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    username: str,
    password: str,
) -> None:
    from app.services import auth_service

    def fail_lookup(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("oversized username reached database lookup")

    def fail_verify(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("oversized credential reached password verification")

    monkeypatch.setattr(
        auth_service.user_repository,
        "get_by_username",
        fail_lookup,
    )
    monkeypatch.setattr(auth_service, "verify_password", fail_verify)
    monkeypatch.setattr(
        auth_service.audit_service,
        "record",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(auth_service.AuthenticationError) as rejected:
        auth_service.login(
            db_session,
            username=username,
            password=password,
        )

    assert rejected.value.status_code == 401
    assert rejected.value.detail == "Incorrect username or password"


def test_missing_and_existing_accounts_each_verify_exactly_once(
    db_session: Session,
    user_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models
    from app.services import auth_service

    existing = user_factory(
        username="timing-existing",
        password="correct-password",
    )
    assert isinstance(existing, models.User)
    monkeypatch.setattr(
        auth_service.audit_service,
        "record",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        auth_service,
        "create_access_token",
        lambda subject: f"token-for-{subject}",
    )

    verification_calls: list[tuple[str, str]] = []

    def verify(raw: str, hashed: str) -> bool:
        verification_calls.append((raw, hashed))
        return hashed == existing.password_hash

    monkeypatch.setattr(auth_service, "verify_password", verify)

    monkeypatch.setattr(
        auth_service.user_repository,
        "get_by_username",
        lambda _db, _username: None,
    )
    with pytest.raises(auth_service.AuthenticationError):
        auth_service.login(
            db_session,
            username="timing-missing",
            password="correct-password",
        )
    assert len(verification_calls) == 1
    assert verification_calls[0][1] != existing.password_hash

    verification_calls.clear()
    monkeypatch.setattr(
        auth_service.user_repository,
        "get_by_username",
        lambda _db, _username: existing,
    )
    token = auth_service.login(
        db_session,
        username=existing.username,
        password="correct-password",
    )
    assert token.access_token == f"token-for-{existing.username}"
    assert len(verification_calls) == 1
    assert verification_calls[0] == (
        "correct-password",
        existing.password_hash,
    )


def test_inactive_account_uses_dummy_hash_and_generic_401(
    db_session: Session,
    user_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models
    from app.services import auth_service

    suspended = user_factory(
        username="suspended-user",
        password="correct-password",
        status="suspended",
    )
    assert isinstance(suspended, models.User)
    monkeypatch.setattr(
        auth_service.user_repository,
        "get_by_username",
        lambda _db, _username: suspended,
    )
    monkeypatch.setattr(
        auth_service.audit_service,
        "record",
        lambda *_args, **_kwargs: None,
    )
    verification_calls: list[tuple[str, str]] = []

    def verify(raw: str, hashed: str) -> bool:
        verification_calls.append((raw, hashed))
        return True

    monkeypatch.setattr(auth_service, "verify_password", verify)

    with pytest.raises(auth_service.AuthenticationError) as rejected:
        auth_service.login(
            db_session,
            username=suspended.username,
            password="correct-password",
        )

    assert rejected.value.status_code == 401
    assert rejected.value.detail == "Incorrect username or password"
    assert len(verification_calls) == 1
    assert verification_calls[0][1] != suspended.password_hash


def test_malformed_stored_hash_fails_safely_after_one_verification(
    db_session: Session,
    user_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models
    from app.services import auth_service

    user = user_factory(username="malformed-hash-user")
    assert isinstance(user, models.User)
    user.password_hash = "$pbkdf2-sha256$recognized-but-malformed"
    monkeypatch.setattr(
        auth_service.user_repository,
        "get_by_username",
        lambda _db, _username: user,
    )
    monkeypatch.setattr(
        auth_service.audit_service,
        "record",
        lambda *_args, **_kwargs: None,
    )
    real_verify = auth_service.verify_password
    verification_calls: list[tuple[str, str]] = []

    def counted_verify(raw: str, hashed: str) -> bool:
        verification_calls.append((raw, hashed))
        return real_verify(raw, hashed)

    monkeypatch.setattr(auth_service, "verify_password", counted_verify)

    with pytest.raises(auth_service.AuthenticationError) as rejected:
        auth_service.login(
            db_session,
            username=user.username,
            password="bounded-password",
        )

    assert rejected.value.status_code == 401
    assert len(verification_calls) == 1
    assert verification_calls[0][1] != "$pbkdf2-sha256$recognized-but-malformed"


def test_login_credentials_and_body_never_enter_logs_or_audit(
    api_client: TestClient,
    rendered_events: _RenderedEventCapture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app import models
    from app.database import SessionLocal

    username_canary = "credential-canary@example.test"
    password_canary = "BodySecretCanary!42"
    with caplog.at_level(logging.DEBUG):
        response = api_client.post(
            LOGIN_PATH,
            data={
                "username": username_canary,
                "password": password_canary,
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Incorrect username or password"}
    rendered = "\n".join(rendered_events.rendered) + caplog.text
    assert username_canary not in rendered
    assert password_canary not in rendered

    db = SessionLocal()
    try:
        audit = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "LOGIN_FAILED")
            .order_by(models.AuditLog.id.desc())
            .first()
        )
        assert audit is not None
        persisted = json.dumps(
            {
                "actor_name": audit.actor_name,
                "object_id": audit.object_id,
                "details": audit.details,
                "user_agent": audit.user_agent,
            },
            sort_keys=True,
        )
    finally:
        db.close()

    assert username_canary not in persisted
    assert password_canary not in persisted
    assert audit.object_id == "redacted"
