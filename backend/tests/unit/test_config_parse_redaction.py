"""Malformed environment settings never expose their raw values."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCALAR_CANARY = "ConfigScalarSecretCanary"
JSON_CANARY = "ConfigJsonSecretCanary"


def _environment(**values: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DOCVAULT_")
    }
    environment.update(values)
    return environment


@pytest.mark.parametrize(
    ("field", "value", "canary"),
    [
        ("DOCVAULT_ACCESS_TOKEN_MINUTES", SCALAR_CANARY, SCALAR_CANARY),
        (
            "DOCVAULT_CORS_ORIGINS",
            f'["https://example.test", "{JSON_CANARY}',
            JSON_CANARY,
        ),
    ],
)
def test_supported_config_launcher_is_traceback_free_and_redacted(
    field: str,
    value: str,
    canary: str,
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.config_check"],
        cwd=BACKEND_DIR,
        env=_environment(**{field: value}),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr == (
        "DocVault startup configuration rejected (CFG_SETTINGS_PARSE).\n"
    )
    assert canary not in result.stderr
    assert "Traceback" not in result.stderr
    assert "input_value" not in result.stderr


def test_direct_config_import_hides_value_even_when_launcher_is_bypassed() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=BACKEND_DIR,
        env=_environment(DOCVAULT_ACCESS_TOKEN_MINUTES=SCALAR_CANARY),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode != 0
    assert SCALAR_CANARY not in result.stderr
    assert "input_value" not in result.stderr
    assert "CFG_SETTINGS_PARSE" in result.stderr
