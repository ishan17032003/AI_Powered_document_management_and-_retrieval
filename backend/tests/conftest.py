"""Shared fixtures that keep every backend test away from checked-in data."""

from __future__ import annotations

import hashlib
import importlib
import secrets
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
SOURCE_DATABASE_FILES = ()
SOURCE_STORAGE = BACKEND_DIR / "storage"
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


@dataclass(frozen=True)
class TestPaths:
    root: Path
    database: Path
    storage: Path
    okf_bundle: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_state() -> dict[str, tuple[int, str]]:
    """Fingerprint protected source data without opening the SQLite database."""
    paths = [path for path in SOURCE_DATABASE_FILES if path.is_file()]
    if SOURCE_STORAGE.is_dir():
        paths.extend(path for path in SOURCE_STORAGE.rglob("*") if path.is_file())
    return {
        str(path.relative_to(BACKEND_DIR)): (path.stat().st_size, _sha256(path))
        for path in sorted(paths)
    }


def _dispose_test_engine() -> None:
    database = sys.modules.get("app.database")
    engine = getattr(database, "engine", None)
    if engine is not None:
        engine.dispose()


def _purge_app_modules() -> None:
    _dispose_test_engine()
    for name in tuple(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)


def _migrate_test_database(db_url: str) -> None:
    """Explicitly migrate one isolated test database to the packaged head."""

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def protect_checked_in_data() -> Iterator[None]:
    """Fail the suite if any test changes the source DB, WAL, SHM, or storage."""
    before = _source_state()
    yield
    after = _source_state()
    assert after == before, "tests modified checked-in backend data"


@pytest.fixture
def test_paths(tmp_path: Path) -> TestPaths:
    storage = tmp_path / "storage"
    okf_bundle = tmp_path / "okf_bundle"
    storage.mkdir()
    okf_bundle.mkdir()
    return TestPaths(
        root=tmp_path,
        database=Path("dummy"), # Unused for postgres but kept for object shape
        storage=storage,
        okf_bundle=okf_bundle,
    )


@pytest.fixture
def migrated_test_database(test_paths: TestPaths) -> str:
    """Provide an explicitly migrated isolated DB for subprocess/CLI tests."""
    import os
    db_url = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@postgres:5432/docvault_test")
    _migrate_test_database(db_url)
    return db_url


@pytest.fixture
def settings_env(
    monkeypatch: pytest.MonkeyPatch,
    test_paths: TestPaths,
) -> Iterator[dict[str, str]]:
    """Configure an offline, lightweight application against temporary paths."""
    _purge_app_modules()
    import os
    db_url = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@postgres:5432/docvault_test")
    values = {
        "DOCVAULT_ENVIRONMENT": "test",
        "DOCVAULT_DATABASE_URL": db_url,
        "DOCVAULT_DEBUG": "false",
        "DOCVAULT_ENABLE_DEMO_SEED": "true",
        "DOCVAULT_STORAGE_DIR": str(test_paths.storage),
        "DOCVAULT_OKF_BUNDLE_DIR": str(test_paths.okf_bundle),
        "DOCVAULT_SECRET_KEY": "test-only-secret-key-with-no-production-value",
        "DOCVAULT_LLM_PROVIDER": "none",
        "DOCVAULT_ENABLE_EMBEDDINGS": "false",
        "DOCVAULT_USE_DOCLING": "false",
        "DOCVAULT_USE_QDRANT": "false",
        "DOCVAULT_EMBEDDING_MODEL": "",
        "DOCVAULT_RERANKER_MODEL": "",
        "DOCVAULT_ANTHROPIC_API_KEY": "",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    yield values
    _purge_app_modules()


@pytest.fixture
def app_factory(settings_env: dict[str, str]) -> Iterator[Callable[[], FastAPI]]:
    """Return a factory for a fully isolated application and initialized DB."""

    def factory() -> FastAPI:
        _purge_app_modules()
        db_url = settings_env["DOCVAULT_DATABASE_URL"]
        _migrate_test_database(db_url)
        main = importlib.import_module("app.main")
        return main.app

    yield factory
    _purge_app_modules()


@pytest.fixture
def demo_credentials() -> dict[str, str]:
    return {
        username: f"DvTest1!{secrets.token_urlsafe(24)}"
        for username in ("admin", "contributor", "viewer", "auditor")
    }


@pytest.fixture
def seeded_app(
    app_factory: Callable[[], FastAPI],
    demo_credentials: dict[str, str],
) -> FastAPI:
    """Create an isolated app with roles and per-test generated users."""
    application = app_factory()
    seed = importlib.import_module("app.seed")
    database = importlib.import_module("app.database")
    db = database.SessionLocal()
    try:
        seed.seed_demo_database(
            db,
            password_factory=lambda username: demo_credentials[username],
        )
    finally:
        db.close()
    return application


@pytest.fixture
def api_client(
    seeded_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Run HTTP requests in-process; no TCP listener or external service is used."""

    def reject_provider_call(*_args, **_kwargs):
        raise AssertionError("API smoke test attempted an outbound provider call")

    monkeypatch.setattr(
        "app.services.rag_service._answer_with_vllm",
        reject_provider_call,
    )
    monkeypatch.setattr(
        "app.services.rag_service._answer_with_ollama",
        reject_provider_call,
    )
    monkeypatch.setattr(
        "app.services.rag_service._get_client",
        reject_provider_call,
    )
    with TestClient(seeded_app, base_url="http://docvault.test") as client:
        yield client


@pytest.fixture
def admin_client(
    api_client: TestClient,
    demo_credentials: dict[str, str],
) -> TestClient:
    response = api_client.post(
        "/api/v1/auth/login",
        data={"username": "admin", "password": demo_credentials["admin"]},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    api_client.headers["Authorization"] = f"Bearer {token}"
    return api_client


@pytest.fixture
def db_session(settings_env: dict[str, str]) -> Iterator[Session]:
    db_url = settings_env["DOCVAULT_DATABASE_URL"]
    _migrate_test_database(db_url)
    database = importlib.import_module("app.database")
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        _purge_app_modules()


@pytest.fixture
def user_factory(db_session: Session) -> Callable[..., object]:
    """Create persisted test users without seeding demo accounts."""
    models = importlib.import_module("app.models")
    security = importlib.import_module("app.utils.security")

    def factory(
        *,
        username: str | None = None,
        name: str = "Test User",
        email: str | None = None,
        password: str = "test-password",
        status: str = "active",
    ) -> object:
        unique = uuid4().hex
        user = models.User(
            username=username or f"user-{unique}",
            name=name,
            email=email or f"{unique}@example.test",
            password_hash=security.hash_password(password),
            status=status,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return factory
