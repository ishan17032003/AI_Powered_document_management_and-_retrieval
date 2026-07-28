"""Backend-wide request-body admission and parser-boundary checks."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.middleware import (
    LOGIN_PATH,
    LoginBodyLimitMiddleware,
    RequestCorrelationMiddleware,
)
from app.request_body_limits import (
    MAX_MULTIPART_OVERHEAD_BYTES,
    MAX_REQUEST_BODY_BYTES,
    RequestBodyLimitMiddleware,
)


def _scope(
    path: str,
    *,
    method: str = "POST",
    headers: Sequence[tuple[bytes, bytes]] = (),
) -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": list(headers),
            "client": ("127.0.0.1", 41234),
            "server": ("docvault.test", 80),
        },
    )


async def _run(
    scope: Scope,
    incoming: list[Message],
) -> tuple[list[Message], list[Message]]:
    sent: list[Message] = []
    forwarded: list[Message] = []
    receive_index = 0

    async def receive() -> Message:
        nonlocal receive_index
        message = incoming[receive_index]
        receive_index += 1
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    async def endpoint(_scope: Scope, app_receive: Receive, app_send: Send) -> None:
        while True:
            message = await app_receive()
            forwarded.append(message)
            if not message.get("more_body", False):
                break
        await app_send(
            {"type": "http.response.start", "status": 204, "headers": []}
        )
        await app_send(
            {"type": "http.response.body", "body": b"", "more_body": False}
        )

    # The helper endpoint is installed by wrapping the middleware under test.
    await RequestBodyLimitMiddleware(
        endpoint,
        max_upload_bytes=100,
    )(scope, receive, send)
    return sent, forwarded


def _response_status(sent: list[Message]) -> int:
    return int(next(message for message in sent if message["type"] == "http.response.start")["status"])


def _response_body(sent: list[Message]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )


def test_declared_generic_body_is_rejected_before_endpoint() -> None:
    sent, forwarded = asyncio.run(
        _run(
            _scope(
                "/api/v1/search",
                headers=[
                    (b"content-length", str(MAX_REQUEST_BODY_BYTES + 1).encode())
                ],
            ),
            [
                {
                    "type": "http.request",
                    "body": b"endpoint-must-not-read",
                    "more_body": False,
                }
            ],
        )
    )

    assert _response_status(sent) == 413
    assert forwarded == []
    assert _response_body(sent) == b'{"detail":"Request body too large"}'


def test_chunked_generic_body_forwards_only_bounded_chunks() -> None:
    first = b"a" * MAX_REQUEST_BODY_BYTES
    sent, forwarded = asyncio.run(
        _run(
            _scope("/api/v1/search", headers=[(b"transfer-encoding", b"chunked")]),
            [
                {"type": "http.request", "body": first, "more_body": True},
                {
                    "type": "http.request",
                    "body": b"credential-or-file-canary",
                    "more_body": True,
                },
                {"type": "http.request", "body": b"unread", "more_body": False},
            ],
        )
    )

    assert _response_status(sent) == 413
    assert [message["body"] for message in forwarded] == [first]
    assert b"credential-or-file-canary" not in _response_body(sent)


def test_upload_exception_is_exact_post_route_and_has_bounded_overhead() -> None:
    upload_limit = 100 + MAX_MULTIPART_OVERHEAD_BYTES
    sent, forwarded = asyncio.run(
        _run(
            _scope(
                "/api/v1/documents",
                headers=[(b"content-length", str(upload_limit).encode())],
            ),
            [{"type": "http.request", "body": b"", "more_body": False}],
        )
    )
    assert _response_status(sent) == 204
    assert len(forwarded) == 1

    sent, forwarded = asyncio.run(
        _run(
            _scope(
                "/api/v1/documents",
                headers=[(b"content-length", str(upload_limit + 1).encode())],
            ),
            [{"type": "http.request", "body": b"must-not-read", "more_body": False}],
        )
    )
    assert _response_status(sent) == 413
    assert forwarded == []

    # A different method/path never inherits the upload allowance.
    sent, forwarded = asyncio.run(
        _run(
            _scope(
                "/api/v1/documents",
                method="GET",
                headers=[(b"content-length", str(MAX_REQUEST_BODY_BYTES + 1).encode())],
            ),
            [{"type": "http.request", "body": b"must-not-read", "more_body": False}],
        )
    )
    assert _response_status(sent) == 413
    assert forwarded == []


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"12"), (b"content-length", b"12")],
        [(b"content-length", b"12,12")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"not-a-number")],
    ],
)
def test_duplicate_or_invalid_content_length_is_rejected(headers: list[tuple[bytes, bytes]]) -> None:
    sent, forwarded = asyncio.run(
        _run(
            _scope("/api/v1/search", headers=headers),
            [{"type": "http.request", "body": b"must-not-read", "more_body": False}],
        )
    )
    assert _response_status(sent) == 400
    assert forwarded == []
    assert _response_body(sent) == b'{"detail":"Invalid Content-Length"}'


def test_login_limit_remains_stricter_without_double_reading() -> None:
    forwarded: list[bytes] = []
    sent: list[Message] = []
    incoming = [
        {
            "type": "http.request",
            "body": b"a" * (16 * 1024),
            "more_body": True,
        },
        {
            "type": "http.request",
            "body": b"password-canary",
            "more_body": False,
        },
    ]
    index = 0

    async def receive() -> Message:
        nonlocal index
        message = incoming[index]
        index += 1
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    async def endpoint(_scope: Scope, app_receive: Receive, _send: Send) -> None:
        while True:
            message = await app_receive()
            forwarded.append(message.get("body", b""))
            if not message.get("more_body", False):
                return

    application: ASGIApp = RequestBodyLimitMiddleware(
        LoginBodyLimitMiddleware(endpoint),
        max_upload_bytes=100,
    )
    async def exercise() -> None:
        await application(_scope(LOGIN_PATH), receive, send)

    asyncio.run(exercise())

    assert _response_status(sent) == 413
    assert forwarded == [b"a" * (16 * 1024)]
    assert index == 2
    assert b"password-canary" not in _response_body(sent)


def test_over_limit_response_keeps_cors_and_correlation_headers() -> None:
    app = FastAPI()
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_upload_bytes=100,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestCorrelationMiddleware)

    called = False

    @app.post("/api/v1/search")
    def endpoint() -> None:
        nonlocal called
        called = True

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/search",
            content=b"not-read",
            headers={
                "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1),
                "Origin": "http://localhost:5173",
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Correlation-ID"]
    assert called is False
