"""Bound request bodies before framework parsers allocate them.

This is deliberately a small ASGI middleware rather than a FastAPI dependency:
form and JSON parsing happen after dependencies are resolved and can otherwise
consume an unbounded request body.  The middleware forwards body chunks directly
and never buffers or logs request content.
"""

from __future__ import annotations

import json
from typing import Final

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .error_responses import error_payload

# Non-upload requests are intentionally conservative.  A request body is an
# input envelope, not a document-storage limit; document uploads use the
# separate settings.max_upload_bytes allowance below.
MAX_REQUEST_BODY_BYTES: Final = 1 * 1024 * 1024
MAX_MULTIPART_OVERHEAD_BYTES: Final = 64 * 1024
UPLOAD_PATH: Final = "/api/v1/documents"



class _RequestBodyTooLarge(Exception):
    """Internal control flow raised without retaining an over-limit chunk."""


class _InvalidContentLength(Exception):
    """Internal control flow for malformed or repeated Content-Length headers."""


def _response(
    send: Send,
    scope: Scope,
    status: int,
    *,
    code: str,
    message: str,
):
    """Send a bounded correlated JSON error response."""

    body = json.dumps(
        error_payload(
            request=Request(scope),
            status_code=status,
            code=code,
            message=message,
        ),
        separators=(",", ":"),
    ).encode("utf-8")

    async def _send() -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": body, "more_body": False}
        )

    return _send()


def _content_length(scope: Scope) -> int | None:
    """Parse one strict Content-Length header, rejecting ambiguity safely.

    ASGI servers expose repeated headers as separate entries.  Repeated values
    are rejected even when equal, avoiding parser/server disagreement.  Decimal
    values are bounded before integer conversion so a hostile header cannot cause
    unbounded work.
    """

    values = [
        raw_value
        for raw_name, raw_value in scope.get("headers", ())
        if raw_name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise _InvalidContentLength

    value = values[0].strip()
    if not value or any(byte < ord("0") or byte > ord("9") for byte in value):
        raise _InvalidContentLength

    # Leading zeroes do not change the value.  Only convert a bounded number of
    # significant digits; a longer value is certainly above either configured
    # body cap and can be represented by a sentinel without integer conversion.
    significant = value.lstrip(b"0") or b"0"
    if len(significant) > 20:
        return MAX_REQUEST_BODY_BYTES + 1
    return int(significant)


MAX_MEDIA_UPLOAD_BYTES: Final = 20 * 1024 * 1024 * 1024  # 20 GB for media uploads


def _limit_for(scope: Scope, *, max_body_bytes: int, max_upload_bytes: int) -> int:
    """Return the finite cap for every HTTP scope, including unknown paths."""

    path = scope.get("path", "")
    if (
        scope.get("method") == "POST"
        and (path == UPLOAD_PATH or path.startswith(f"{UPLOAD_PATH}/"))
    ):
        effective_upload = max(max_upload_bytes, MAX_MEDIA_UPLOAD_BYTES)
        return effective_upload + MAX_MULTIPART_OVERHEAD_BYTES
    return max_body_bytes


class RequestBodyLimitMiddleware:
    """Apply a bounded body cap before FastAPI/Pydantic parsing.

    The only larger allowance is the exact document upload route.  All other
    methods and paths, including unknown endpoints, receive the conservative
    default cap.  Body chunks are counted and forwarded without being copied.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_REQUEST_BODY_BYTES,
        max_upload_bytes: int,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_upload_bytes = max_upload_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            declared_length = _content_length(scope)
        except _InvalidContentLength:
            await _response(
                send,
                scope,
                400,
                code="invalid_content_length",
                message="Invalid Content-Length",
            )
            return

        limit = _limit_for(
            scope,
            max_body_bytes=self.max_body_bytes,
            max_upload_bytes=self.max_upload_bytes,
        )
        if declared_length is not None and declared_length > limit:
            await _response(
                send,
                scope,
                413,
                code="payload_too_large",
                message="The request body is too large",
            )
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
            if accepted_bytes > limit:
                # Do not forward or retain the over-limit chunk.  In particular,
                # this avoids copying credentials or document bytes into a
                # middleware-owned buffer.
                raise _RequestBodyTooLarge
            return message

        async def track_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, track_response)
        except _RequestBodyTooLarge:
            # Normal FastAPI parsers read the body before producing a response.
            # If a nonconforming downstream app already started its response,
            # do not attempt a second response start.
            if response_started:
                raise
            await _response(
                send,
                scope,
                413,
                code="payload_too_large",
                message="The request body is too large",
            )


__all__ = [
    "MAX_MULTIPART_OVERHEAD_BYTES",
    "MAX_REQUEST_BODY_BYTES",
    "RequestBodyLimitMiddleware",
    "UPLOAD_PATH",
]
