"""Validate the reviewable visual-model BOM without downloading model artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def validate(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "docvault.visual-model-bom.v1":
        raise ValueError("unsupported visual BOM schema")
    if payload.get("release_policy", {}).get("remote_code") is not False:
        raise ValueError("visual BOM must disable remote code")
    for model in payload.get("models", []):
        digest = model.get("artifact_sha256", "")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("approved model is missing a SHA-256 artifact digest")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    path = Path(__file__).resolve().parents[1] / "artifacts" / "visual_model_bom.json"
    print(json.dumps({"valid": True, "sha256": validate(path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
