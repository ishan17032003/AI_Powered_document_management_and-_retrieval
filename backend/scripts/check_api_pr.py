"""Validate pull-request API classification against the OpenAPI snapshot diff."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
OPENAPI_SNAPSHOT = Path("backend/openapi/openapi.json")
CLASSIFICATION_PATTERN = re.compile(
    r"(?im)^\s*API change classification:\s*`?"
    r"(none|additive|breaking)`?\s*$"
)


def validate_classification(body: str, *, snapshot_changed: bool) -> str:
    """Return the single declared classification or raise a policy error."""
    classifications = CLASSIFICATION_PATTERN.findall(body)
    if len(classifications) != 1:
        raise ValueError(
            "pull-request body must contain exactly one line formatted as "
            "'API change classification: none|additive|breaking'"
        )

    classification = classifications[0].lower()
    if snapshot_changed and classification == "none":
        raise ValueError(
            "the OpenAPI snapshot changed, so API classification must be "
            "'additive' or 'breaking'"
        )
    return classification


def snapshot_changed(base_sha: str) -> bool:
    """Return whether the reviewed snapshot differs from the pull-request base."""
    result = subprocess.run(
        [
            "git",
            "diff",
            "--quiet",
            base_sha,
            "--",
            str(OPENAPI_SNAPSHOT),
        ],
        cwd=PROJECT_DIR,
        check=False,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise RuntimeError(f"git diff failed with exit code {result.returncode}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate pull-request API contract classification."
    )
    parser.add_argument(
        "--base",
        required=True,
        help="base commit SHA available in the fully fetched checkout",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    body = os.environ.get("DOCVAULT_PR_BODY", "")
    try:
        changed = snapshot_changed(args.base)
        classification = validate_classification(body, snapshot_changed=changed)
    except (RuntimeError, ValueError) as exc:
        print(f"API contract policy check failed: {exc}", file=sys.stderr)
        return 1

    print(
        "API contract policy check passed "
        f"(classification={classification}, snapshot_changed={str(changed).lower()})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
