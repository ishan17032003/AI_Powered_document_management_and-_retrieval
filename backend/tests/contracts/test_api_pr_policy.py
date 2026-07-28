"""Tests for pull-request API contract classification."""

from __future__ import annotations

import pytest

from scripts.check_api_pr import validate_classification


@pytest.mark.parametrize("classification", ["additive", "breaking"])
def test_changed_snapshot_requires_compatible_classification(
    classification: str,
) -> None:
    body = f"API change classification: {classification}"
    assert validate_classification(body, snapshot_changed=True) == classification


def test_unchanged_snapshot_accepts_explicit_none() -> None:
    assert (
        validate_classification(
            "API change classification: none",
            snapshot_changed=False,
        )
        == "none"
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "API change classification: unknown",
        ("API change classification: additive\nAPI change classification: breaking"),
    ],
)
def test_classification_must_be_present_valid_and_unique(body: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        validate_classification(body, snapshot_changed=False)


def test_changed_snapshot_rejects_none() -> None:
    with pytest.raises(ValueError, match="snapshot changed"):
        validate_classification(
            "API change classification: none",
            snapshot_changed=True,
        )
