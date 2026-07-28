"""Safe, correlated HTTP error responses.

The API boundary is the only place where application exceptions become wire
responses.  This module deliberately keeps the response contract small:
``code`` and ``message`` are stable, ``request_id`` is the server-generated
opaque request identifier, and ``field_errors`` contains a bounded projection
of validation failures.  Exception text, input values, paths, and credentials
never cross this boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .services.exceptions import ServiceError
from .utils.request_context import get_request_context

MAX_FIELD_ERRORS: Final = 20
MAX_FIELD_LENGTH: Final = 128
MAX_MESSAGE_LENGTH: Final = 256

_FIELD_PART = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_TEXT = re.compile(
    r"(?:password|passwd|secret|token|authorization|bearer|credential|"
    r"api[-_ ]?key|/|\\|[A-Za-z]:\\)",
    re.IGNORECASE,
)

_STATUS_CODES: Final[dict[int, str]] = {
    400: "bad_request",
    401: "authentication_required",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

_STATUS_MESSAGES: Final[dict[int, str]] = {
    400: "The request could not be processed",
    401: "Authentication is required",
    403: "You do not have permission to perform this action",
    404: "The requested resource was not found",
    405: "The requested method is not allowed",
    409: "The request conflicts with the current resource state",
    410: "The requested resource is no longer available",
    413: "The request body is too large",
    415: "The request media type is not supported",
    422: "Request validation failed",
    429: "Too many requests",
    500: "Internal server error",
    503: "The service is temporarily unavailable",
}

_VALIDATION_MESSAGES: Final[dict[str, str]] = {
    "missing": "Field is required",
    "missing_argument": "Field is required",
    "missing_keyword_only_argument": "Field is required",
    "missing_positional_only_argument": "Field is required",
    "string_too_short": "Value is too short",
    "string_too_long": "Value is too long",
    "bytes_too_short": "Value is too short",
    "bytes_too_long": "Value is too long",
    "greater_than": "Value is too small",
    "greater_than_equal": "Value is too small",
    "less_than": "Value is too large",
    "less_than_equal": "Value is too large",
    "int_parsing": "Value must be an integer",
    "float_parsing": "Value must be a number",
    "bool_parsing": "Value must be a boolean",
    "date_from_datetime_parsing": "Value must be a valid date",
    "datetime_from_date_parsing": "Value must be a valid datetime",
    "datetime_parsing": "Value must be a valid datetime",
    "enum": "Value is not an allowed option",
    "literal_error": "Value is not an allowed option",
    "json_invalid": "Value must be valid JSON",
    "extra_forbidden": "Unexpected field",
    "model_type": "Value has an invalid type",
    "list_type": "Value must be a list",
    "dict_type": "Value must be an object",
    "string_type": "Value must be a string",
    "bytes_type": "Value must be bytes",
}


def _bounded_message(value: object, *, fallback: str) -> str:
    """Return a short public message, rejecting text likely to contain input."""

    if not isinstance(value, str):
        return fallback
    message = _CONTROL_CHARS.sub(" ", value).strip()
    if not message or len(message) > MAX_MESSAGE_LENGTH:
        return fallback
    if _SENSITIVE_TEXT.search(message) is not None:
        return fallback
    return message


def _request_id(request: Request) -> str:
    # Body-admission middleware runs before a Request object is created by
    # FastAPI.  Prefer its currently bound context when request.state has not
    # been initialized, so the body and response headers share one ID.
    state_context = getattr(request.state, "docvault_request_context", None)
    context = (
        state_context
        if state_context is not None
        else get_request_context(None)
    )
    # Handlers are normally reached under RequestCorrelationMiddleware.  The
    # fallback keeps direct TestClient/FastAPI handler tests deterministic and
    # never echoes an untrusted header.
    return context.request_id or "unavailable"


def _code_for_status(status_code: int) -> str:
    return _STATUS_CODES.get(status_code, "http_error")


def _message_for_status(status_code: int) -> str:
    return _STATUS_MESSAGES.get(status_code, "The request could not be processed")


def _safe_field(value: object) -> str:
    parts: list[str] = []
    if isinstance(value, (tuple, list)):
        values: Iterable[object] = value
    else:
        values = (value,)
    for item in values:
        if isinstance(item, int) and not isinstance(item, bool):
            part = str(item)
        elif isinstance(item, str) and _FIELD_PART.fullmatch(item):
            part = item
        else:
            part = "field"
        parts.append(part)
    field = ".".join(parts) or "field"
    return field[:MAX_FIELD_LENGTH]


def _field_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for error in exc.errors()[:MAX_FIELD_ERRORS]:
        location = error.get("loc", ())
        if isinstance(location, (tuple, list)) and location:
            # ``body``/``query``/``path`` are useful, bounded location labels.
            field = _safe_field(location)
        else:
            field = "request"
        error_type = error.get("type")
        message = _VALIDATION_MESSAGES.get(
            error_type if isinstance(error_type, str) else "",
            "Value is invalid",
        )
        result.append({"field": field, "message": message})
    return result


def error_payload(
    *,
    request: Request,
    status_code: int,
    code: str | None = None,
    message: object = None,
    field_errors: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build the canonical bounded response payload."""

    fallback = _message_for_status(status_code)
    return {
        "code": code or _code_for_status(status_code),
        "message": _bounded_message(message, fallback=fallback),
        "request_id": _request_id(request),
        "field_errors": (field_errors or [])[:MAX_FIELD_ERRORS],
    }


def error_response(
    *,
    request: Request,
    status_code: int,
    code: str | None = None,
    message: object = None,
    field_errors: list[dict[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=error_payload(
            request=request,
            status_code=status_code,
            code=code,
            message=message,
            field_errors=field_errors,
        ),
    )
    if headers:
        for name, value in headers.items():
            # Preserve protocol headers such as WWW-Authenticate, but never
            # copy arbitrary exception metadata into the response.
            if name.lower() in {"www-authenticate", "allow", "retry-after"}:
                response.headers[name] = value[:128]
    return response


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.detail,
    )


async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return error_response(
        request=request,
        status_code=422,
        code="validation_error",
        message="Request validation failed",
        field_errors=_field_errors(exc),
    )


async def http_error_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    return error_response(
        request=request,
        status_code=exc.status_code,
        message=exc.detail,
        headers=exc.headers,
    )


async def unhandled_error_handler(request: Request, _: Exception) -> JSONResponse:
    return error_response(
        request=request,
        status_code=500,
        code="internal_error",
        message="Internal server error",
    )


__all__ = [
    "MAX_FIELD_ERRORS",
    "MAX_FIELD_LENGTH",
    "MAX_MESSAGE_LENGTH",
    "error_payload",
    "error_response",
    "http_error_handler",
    "service_error_handler",
    "unhandled_error_handler",
    "validation_error_handler",
]
