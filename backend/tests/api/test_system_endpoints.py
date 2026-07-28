"""Behavioral coverage for operational endpoint separation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_live_ready_and_status_authorization(
    admin_client: TestClient,
) -> None:
    live = admin_client.get("/api/v1/live")
    assert live.status_code == 200
    assert live.json()["status"] == "live"

    legacy = admin_client.get("/api/v1/health")
    assert legacy.status_code == 200
    assert legacy.json()["status"] == "ok"
    assert set(legacy.json()["ocr_engines"]) == {
        "docling",
        "tesseract",
        "pymupdf",
        "pypdf",
    }

    ready = admin_client.get("/api/v1/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}

    authorization = admin_client.headers.pop("Authorization")
    assert admin_client.get("/api/v1/status").status_code == 401
    admin_client.headers["Authorization"] = authorization

    response = admin_client.get("/api/v1/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["dependencies"] == {
        "database": True,
        "storage": True,
        "okf_bundle": True,
    }
    assert payload["rag"]["provider_policy"]["provider"] == "none"


def test_readiness_returns_503_without_leaking_dependency_detail(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import system_service

    monkeypatch.setattr(
        system_service,
        "readiness_checks",
        lambda: {"database": False, "storage": True, "okf_bundle": True},
    )

    response = api_client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_schema_drift_revokes_readiness_but_not_liveness(
    api_client: TestClient,
) -> None:
    from app.runtime import settings

    database = Path(settings.database_url.removeprefix("sqlite:///"))
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ix_users_username")

    live = api_client.get("/api/v1/live")
    ready = api_client.get("/api/v1/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "live"
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready"}
    assert "schema" not in ready.text.lower()
    assert str(database) not in ready.text


def test_web_startup_does_not_warm_heavy_models(
    app_factory: Callable[[], FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = app_factory()
    from app.services import extraction_service, search_service

    def reject_warm() -> None:
        raise AssertionError("web startup attempted heavyweight model warming")

    monkeypatch.setattr(search_service, "warm_models", reject_warm)
    monkeypatch.setattr(extraction_service, "warm_docling", reject_warm)

    with TestClient(application, base_url="http://docvault.test") as client:
        assert client.get("/api/v1/live").status_code == 200
