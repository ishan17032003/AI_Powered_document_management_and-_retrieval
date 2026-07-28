"""Contract tests for the explicit DocVault runtime environment."""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("raw_value", "member_name"),
    [
        ("development", "DEVELOPMENT"),
        ("test", "TEST"),
        ("production", "PRODUCTION"),
    ],
)
def test_environment_accepts_only_canonical_modes(
    raw_value: str,
    member_name: str,
    monkeypatch: pytest.MonkeyPatch,
    settings_env: dict[str, str],
) -> None:
    config = importlib.import_module("app.config")
    monkeypatch.setenv("DOCVAULT_ENVIRONMENT", raw_value)

    configured = config.Settings(_env_file=None)

    assert configured.environment is getattr(config.RuntimeEnvironment, member_name)
    assert configured.is_development is (raw_value == "development")
    assert configured.is_test is (raw_value == "test")
    assert configured.is_production is (raw_value == "production")


def test_environment_defaults_to_development_for_local_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    settings_env: dict[str, str],
) -> None:
    config = importlib.import_module("app.config")
    monkeypatch.delenv("DOCVAULT_ENVIRONMENT")

    configured = config.Settings(_env_file=None)

    assert configured.environment is config.RuntimeEnvironment.DEVELOPMENT
    assert configured.is_development is True


@pytest.mark.parametrize(
    "invalid_value", ["", "dev", "prod", "PRODUCTION", " production "]
)
def test_environment_rejects_aliases_case_drift_and_empty_values(
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
    settings_env: dict[str, str],
) -> None:
    config = importlib.import_module("app.config")
    monkeypatch.setenv("DOCVAULT_ENVIRONMENT", invalid_value)

    with pytest.raises(ValidationError, match="environment"):
        config.Settings(_env_file=None)


def test_legacy_dev_switch_cannot_select_a_runtime_mode(
    monkeypatch: pytest.MonkeyPatch,
    settings_env: dict[str, str],
) -> None:
    config = importlib.import_module("app.config")
    monkeypatch.delenv("DOCVAULT_ENVIRONMENT")
    monkeypatch.setenv("DOCVAULT_DEV", "1")

    configured = config.Settings(_env_file=None)

    assert configured.environment is config.RuntimeEnvironment.DEVELOPMENT
