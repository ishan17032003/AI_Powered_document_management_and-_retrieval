"""Proof that the shared pytest fixtures use only temporary application data."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session


def test_settings_use_temporary_paths(
    settings_env: dict[str, str],
    test_paths,
) -> None:
    from app.config import RuntimeEnvironment, settings

    assert settings.environment is RuntimeEnvironment.TEST
    assert settings.is_test is True
    assert settings.is_development is False
    assert settings.is_production is False
    assert settings.database_url == f"sqlite:///{test_paths.database}"
    assert settings.storage_dir == test_paths.storage
    assert settings.okf_bundle_dir == test_paths.okf_bundle
    assert settings.llm_provider == "none"
    assert settings.use_docling is False
    assert settings.use_qdrant is False
    assert settings.embedding_model == ""
    assert settings.reranker_model == ""


def test_database_session_is_temporary(
    db_session: Session,
    test_paths,
) -> None:
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1
    assert test_paths.database.is_file()
    assert Path(test_paths.database).resolve().is_relative_to(test_paths.root.resolve())


def test_user_factory_creates_user(
    db_session: Session,
    user_factory,
) -> None:
    user = user_factory(username="fixture-user", email="fixture-user@example.test")

    assert user.id is not None
    assert user.username == "fixture-user"
    assert db_session.get(type(user), user.id) is user


def test_application_factory_is_isolated(
    app_factory,
    test_paths,
) -> None:
    app = app_factory()
    from app.config import settings

    assert isinstance(app, FastAPI)
    assert settings.database_url == f"sqlite:///{test_paths.database}"
    assert test_paths.database.is_file()
