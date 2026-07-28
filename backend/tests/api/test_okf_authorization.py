"""Capability checks for shared OKF knowledge management."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from uuid import uuid4

import pytest
from httpx import Response
from starlette.testclient import TestClient


def _create_capability_identity(
    permission_codes: Iterable[str],
    *,
    scope_type: str = "GLOBAL",
    scope_id: int | None = None,
    effect: str = "ALLOW",
) -> tuple[str, str]:
    from app import models
    from app.database import SessionLocal
    from app.utils.security import hash_password

    unique = uuid4().hex
    username = f"capability-{unique}"
    password = f"DvTest1!{secrets.token_urlsafe(24)}"
    db = SessionLocal()
    try:
        permissions = {
            permission.code: permission
            for permission in db.query(models.Permission).all()
        }
        requested_codes = set(permission_codes)
        assert requested_codes <= permissions.keys()

        role = models.Role(
            name=f"opaque-{unique}",
            description="Isolated capability test bundle",
        )
        user = models.User(
            username=username,
            name="Capability Test User",
            email=f"{unique}@example.test",
            password_hash=hash_password(password),
            status="active",
        )
        db.add_all([role, user])
        db.flush()
        db.add_all(
            models.RolePermission(
                role_id=role.id,
                permission_id=permissions[code].id,
            )
            for code in requested_codes
        )
        db.add(
            models.Assignment(
                user_id=user.id,
                role_id=role.id,
                scope_type=scope_type,
                scope_id=scope_id,
                effect=effect,
            )
        )
        db.commit()
    finally:
        db.close()
    return username, password


def _authorization_headers(
    client: TestClient,
    credentials: tuple[str, str],
) -> dict[str, str]:
    username, password = credentials
    response = client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _assert_forbidden(response: Response) -> None:
    assert response.status_code == 403, response.text
    assert response.json() == {
        "detail": "Missing required permission: MANAGE_KNOWLEDGE"
    }


def test_create_capability_cannot_mutate_shared_knowledge(
    api_client: TestClient,
    demo_credentials: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation_attempted = False

    def reject_mutation(*_args, **_kwargs):
        nonlocal mutation_attempted
        mutation_attempted = True
        raise AssertionError("authorization must run before OKF mutation")

    monkeypatch.setattr("app.services.okf_service.create_entry", reject_mutation)
    monkeypatch.setattr("app.services.okf_service.reload_bundle", reject_mutation)

    create_headers = _authorization_headers(
        api_client,
        _create_capability_identity({"CREATE"}),
    )
    _assert_forbidden(
        api_client.post(
            "/api/v1/search/okf/entries",
            headers=create_headers,
            json={"filename": "blocked.md", "content": "# Must not be written"},
        )
    )
    _assert_forbidden(
        api_client.post(
            "/api/v1/search/okf/reload",
            headers=create_headers,
        )
    )
    assert mutation_attempted is False

    viewer_headers = _authorization_headers(
        api_client,
        ("viewer", demo_credentials["viewer"]),
    )
    assert (
        api_client.get("/api/v1/search/okf/status", headers=viewer_headers).status_code
        == 200
    )
    assert (
        api_client.get("/api/v1/search/okf/entries", headers=viewer_headers).status_code
        == 200
    )


@pytest.mark.parametrize(
    ("scope_type", "scope_id"),
    [
        ("CABINET", 21),
        ("FOLDER", 22),
        ("DOC", 23),
        ("GLOBAL", 24),
    ],
)
def test_resource_scoped_manage_capability_is_forbidden_at_http_boundary(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    scope_type: str,
    scope_id: int,
) -> None:
    mutation_attempted = False

    def reject_mutation(*_args, **_kwargs):
        nonlocal mutation_attempted
        mutation_attempted = True
        raise AssertionError("global authorization must run before OKF mutation")

    monkeypatch.setattr("app.services.okf_service.reload_bundle", reject_mutation)
    headers = _authorization_headers(
        api_client,
        _create_capability_identity(
            {"MANAGE_KNOWLEDGE"},
            scope_type=scope_type,
            scope_id=scope_id,
        ),
    )

    _assert_forbidden(
        api_client.post(
            "/api/v1/search/okf/reload",
            headers=headers,
        )
    )
    assert mutation_attempted is False


def test_manage_knowledge_capability_allows_mutation_without_role_name_logic(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_create(filename: str, content: str) -> dict:
        calls.append(("create", filename, content))
        return {
            "status": "saved",
            "filename": filename,
            "title": "Managed entry",
            "bundle_size": 1,
        }

    def fake_reload() -> int:
        calls.append(("reload",))
        return 1

    monkeypatch.setattr("app.services.okf_service.create_entry", fake_create)
    monkeypatch.setattr("app.services.okf_service.reload_bundle", fake_reload)

    manage_headers = _authorization_headers(
        api_client,
        _create_capability_identity({"MANAGE_KNOWLEDGE"}),
    )
    content = "---\ntitle: Managed entry\n---\nApproved content."
    created = api_client.post(
        "/api/v1/search/okf/entries",
        headers=manage_headers,
        json={"filename": "managed.md", "content": content},
    )
    assert created.status_code == 201, created.text
    assert created.json()["filename"] == "managed.md"

    reloaded = api_client.post(
        "/api/v1/search/okf/reload",
        headers=manage_headers,
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json() == {"status": "reloaded", "entry_count": 1}
    assert calls == [
        ("create", "managed.md", content),
        ("reload",),
    ]

    assert (
        api_client.get("/api/v1/search/okf/status", headers=manage_headers).status_code
        == 403
    )
    assert (
        api_client.get("/api/v1/search/okf/entries", headers=manage_headers).status_code
        == 403
    )
