"""HTTP middleware for safe request and correlation identifiers."""

from __future__ import annotations

import json
import logging
import time
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .error_responses import error_payload
from .observability import emit_event, trace_span
from .utils.request_context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    bound_request_context,
    get_request_context,
    install_request_context,
)

LOGIN_PATH: Final = "/api/v1/auth/login"
MAX_LOGIN_BODY_BYTES: Final = 16 * 1024


class _LoginBodyTooLarge(Exception):
    """Internal control flow used before FastAPI can parse a login form."""


def _declared_content_length(scope: Scope) -> int | None:
    """Return the largest valid Content-Length without trusting duplicates."""

    declared: int | None = None
    for name, raw_value in scope.get("headers", ()):
        if name.lower() != b"content-length":
            continue
        value = raw_value.strip()
        if not value.isdigit():
            continue
        # Avoid converting an attacker-controlled, arbitrarily large decimal.
        length = MAX_LOGIN_BODY_BYTES + 1 if len(value) > 20 else int(value)
        declared = length if declared is None else max(declared, length)
    return declared


async def _send_payload_too_large(send: Send, scope: Scope) -> None:
    body = json.dumps(
        error_payload(
            request=Request(scope),
            status_code=413,
            code="payload_too_large",
            message="The request body is too large",
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (
                    b"content-length",
                    str(len(body)).encode("ascii"),
                ),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )


class LoginBodyLimitMiddleware:
    """Bound only the OAuth2 login body before form parsing can allocate it."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_LOGIN_BODY_BYTES,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != LOGIN_PATH
        ):
            await self.app(scope, receive, send)
            return

        declared_length = _declared_content_length(scope)
        if declared_length is not None and declared_length > self.max_body_bytes:
            await _send_payload_too_large(send, scope)
            return

        accepted_bytes = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal accepted_bytes
            message = await receive()
            if message["type"] != "http.request":
                return message
            body = message.get("body", b"")
            accepted_bytes += len(body)
            if accepted_bytes > self.max_body_bytes:
                # The over-limit chunk is never forwarded or copied into a
                # middleware buffer. FastAPI's form parser therefore sees at
                # most ``max_body_bytes`` bytes.
                raise _LoginBodyTooLarge
            return message

        async def track_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, track_response)
        except _LoginBodyTooLarge:
            if response_started:
                raise
            await _send_payload_too_large(send, scope)


def _route_template(scope: Scope) -> str | None:
    route = scope.get("route")
    template = getattr(route, "path", None)
    return template if isinstance(template, str) else None


def _outcome(status_code: int, error: BaseException | None) -> str:
    if error is not None or status_code >= 500:
        return "error"
    if status_code >= 400:
        return "rejected"
    return "success"


class RequestCorrelationMiddleware:
    """Bind isolated request context and return safe correlation headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        context = install_request_context(request)
        started = time.monotonic()
        status_code = 500
        response_started = False
        response_complete = False
        failure: BaseException | None = None

        async def send_with_ids(message: Message) -> None:
            nonlocal response_complete, response_started, status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_started = True
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = context.request_id
                headers[CORRELATION_ID_HEADER] = context.correlation_id
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete = True
            await send(message)

        with bound_request_context(context):
            emit_event("http.request.started", context=context, component="api", method=scope.get("method"))
            try:
                with trace_span("api", "http_request", context=context):
                    await self.app(scope, receive, send_with_ids)
            except Exception as exc:
                failure = exc
                # Handle application exceptions inside the correlation
                # boundary. Re-raising would let the process server emit a
                # traceback containing arbitrary exception text and would make
                # the outer 500 response lose the correlation headers.
                if not response_started:
                    body = json.dumps(
                        error_payload(
                            request=request,
                            status_code=500,
                            code="internal_error",
                            message="Internal server error",
                        ),
                        separators=(",", ":"),
                    ).encode("utf-8")
                    await send_with_ids(
                        {
                            "type": "http.response.start",
                            "status": 500,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii")),
                            ],
                        }
                    )
                    await send_with_ids(
                        {
                            "type": "http.response.body",
                            "body": body,
                            "more_body": False,
                        }
                    )
                elif not response_complete:
                    try:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b"",
                                "more_body": False,
                            }
                        )
                    except Exception:
                        response_complete = True
            finally:
                duration_ms = (time.monotonic() - started) * 1000
                final_context = get_request_context(request)
                emit_event(
                    "http.request.completed",
                    level=(
                        logging.ERROR
                        if failure is not None or status_code >= 500
                        else logging.INFO
                    ),
                    context=final_context,
                    component="api",
                    outcome=_outcome(status_code, failure),
                    status_code=status_code,
                    duration_ms=duration_ms,
                    method=scope.get("method"),
                    route=_route_template(scope),
                    error=failure,
                )


__all__ = [
    "LOGIN_PATH",
    "MAX_LOGIN_BODY_BYTES",
    "LoginBodyLimitMiddleware",
    "RequestCorrelationMiddleware",
]
