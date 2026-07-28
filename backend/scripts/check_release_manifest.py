"""Fail-closed validation for the frozen release manifest and Compose inputs."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release" / "configuration-manifest.json"
HEX_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def validate(path: Path = MANIFEST) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if data.get("status") != "FROZEN":
        errors.append("status must be FROZEN")
    for name, image in data.get("images", {}).items():
        if name == "base_images":
            values = image
        else:
            values = [image]
        for value in values:
            if "@" not in value or not HEX_DIGEST.fullmatch(value.rsplit("@", 1)[1]):
                errors.append(f"{name} is not digest pinned")
    for name, model in data.get("models", {}).items():
        if model.get("provider") == "none":
            continue
        if model.get("revision") in (None, "", "REQUIRED"):
            errors.append(f"{name} model revision is missing")
        digest = model.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            errors.append(f"{name} model sha256 is missing")
    for key, value in data.get("verification", {}).items():
        if key != "approvals" and (not isinstance(value, str) or value in ("", "REQUIRED")):
            errors.append(f"verification.{key} is missing")
    return errors


if __name__ == "__main__":
    failures = validate()
    if failures:
        for failure in failures:
            print(f"RELEASE_INVALID: {failure}")
        raise SystemExit(78)
    print("release manifest is complete and digest pinned")
