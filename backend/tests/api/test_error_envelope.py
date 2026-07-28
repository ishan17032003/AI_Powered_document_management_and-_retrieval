"""Contract tests for the bounded, correlated API error envelope."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient


def _assert_envelope(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    payload = response.json()
    assert payload["code"] == code
    assert isinstance(payload["message"], str)
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert payload["field_errors"] == []
    assert response.headers["X-Correlation-ID"]
    return payload


def test_service_and_http_errors_use_safe_envelope(
    api_client: TestClient,
    admin_client: TestClient,
) -> None:
    unauthenticated = _assert_envelope(
        api_client.get("/api/v1/admin/users"),
        401,
        "authentication_required",
    )
    assert "detail" not in unauthenticated

    forbidden = _assert_envelope(
        admin_client.post(
            "/api/v1/documents/import-folder",
            json={"path": "/not-allowed", "recursive": True},
        ),
        403,
        "forbidden",
    )
    assert "not-allowed" not in forbidden["message"]

    not_found = _assert_envelope(
        admin_client.get("/api/v1/documents/999999"),
        404,
        "not_found",
    )
    assert "999999" not in not_found["message"]


def test_validation_errors_are_bounded_and_do_not_echo_values(
    admin_client: TestClient,
) -> None:
    canary = "secret-value-should-not-return"
    response = admin_client.post(
        "/api/v1/search/semantic",
        json={"q": canary, "limit": 0, "unexpected": canary},
    )
    payload = _assert_envelope(response, 422, "validation_error")
    assert payload["message"] == "Request validation failed"
    assert len(payload["field_errors"]) <= 20
    assert all(set(error) == {"field", "message"} for error in payload["field_errors"])
    assert canary not in response.text


def test_body_limit_errors_have_the_same_request_id(
    api_client: TestClient,
) -> None:
    response = api_client.post(
        "/api/v1/search",
        headers={"Content-Length": str(2 * 1024 * 1024)},
        content=b"",
    )
    _assert_envelope(response, 413, "payload_too_large")


def test_unexpected_errors_are_generic_and_correlated(
    api_client: TestClient,
    monkeypatch,
) -> None:
    from app.services import system_service

    monkeypatch.setattr(
        system_service,
        "readiness_checks",
        lambda: (_ for _ in ()).throw(RuntimeError("/private/path and secret")),
    )
    response = api_client.get("/api/v1/ready")
    payload = _assert_envelope(response, 500, "internal_error")
    assert payload["message"] == "Internal server error"
    assert "/private/path" not in response.text
    assert "secret" not in response.text


def test_asgi_transport_exercises_http_envelope_without_lifespan(
    seeded_app,
) -> None:
    """Exercise the wire contract when the sandbox cannot run lifespan startup.

    ``httpx.ASGITransport`` deliberately skips lifespan.  This keeps the
    contract test focused on middleware, routing, authentication, and error
    handlers while the normal TestClient tests continue to cover a full
    deployment where startup is available.
    """

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=seeded_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://docvault.test",
        ) as client:
            response = await client.get("/api/v1/admin/users")
            payload = _assert_envelope(response, 401, "authentication_required")
            assert "detail" not in payload

            oversized = await client.post(
                "/api/v1/search",
                headers={"Content-Length": str(2 * 1024 * 1024)},
                content=b"",
            )
            _assert_envelope(oversized, 413, "payload_too_large")

    asyncio.run(exercise())
