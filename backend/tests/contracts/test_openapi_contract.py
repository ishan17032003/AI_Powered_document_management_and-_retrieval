"""Regression gate for the public HTTP contract."""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI

from scripts import export_openapi
from scripts.export_openapi import (
    DEFAULT_SNAPSHOT,
    canonical_json_bytes,
    canonical_openapi_bytes,
)


def test_openapi_contract_matches_reviewed_snapshot(
    app_factory: Callable[[], FastAPI],
) -> None:
    application = app_factory()
    expected = DEFAULT_SNAPSHOT.read_bytes()
    current = canonical_openapi_bytes(application)

    difference = "".join(
        difflib.unified_diff(
            expected.decode("utf-8").splitlines(keepends=True),
            current.decode("utf-8").splitlines(keepends=True),
            fromfile=str(DEFAULT_SNAPSHOT),
            tofile="current application contract",
        )
    )
    assert current == expected, (
        "OpenAPI contract drifted. Classify the change under "
        "docs/api-contract-policy.md and update the snapshot only after review.\n"
        f"{difference}"
    )

    schema = json.loads(expected)
    assert expected == canonical_json_bytes(schema), "snapshot is not canonical JSON"
    assert schema["openapi"].startswith("3.1.")
    assert schema["info"]["version"] == "0.1.0"
    assert schema["paths"]
    assert all(path.startswith("/api/v1/") for path in schema["paths"])

    request_schemas = schema["components"]["schemas"]
    expected_bounds = {
        "ImportRequest": ("path", 1, 4096),
        "SemanticQuery": ("q", 1, 2000),
        "AskQuery": ("question", 1, 4000),
        "OkfEntryCreate": ("filename", 1, 255),
    }
    for model_name, (field, minimum, maximum) in expected_bounds.items():
        model = request_schemas[model_name]
        assert model["additionalProperties"] is False
        assert model["properties"][field]["minLength"] == minimum
        assert model["properties"][field]["maxLength"] == maximum

    assert request_schemas["SemanticQuery"]["properties"]["limit"] == {
        "default": 20,
        "maximum": 100.0,
        "minimum": 1.0,
        "title": "Limit",
        "type": "integer",
    }
    assert request_schemas["ResolveDup"]["properties"]["action"]["enum"] == [
        "keep_primary",
        "keep_both",
    ]

    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method
        in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_openapi_check_rejects_contract_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = tmp_path / "openapi.json"
    snapshot.write_bytes(
        canonical_json_bytes({"info": {"version": "reviewed-version"}})
    )
    monkeypatch.setattr(
        export_openapi,
        "render_isolated_openapi",
        lambda: canonical_json_bytes({"info": {"version": "drifted-version"}}),
    )

    assert export_openapi.main(["--check", "--output", str(snapshot)]) == 1
    error = capsys.readouterr().err
    assert "OpenAPI contract drift detected" in error
    assert '"version": "reviewed-version"' in error
    assert '"version": "drifted-version"' in error
