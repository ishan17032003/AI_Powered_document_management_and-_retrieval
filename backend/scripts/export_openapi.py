"""Generate or verify DocVault's canonical OpenAPI contract snapshot."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

from fastapi import FastAPI

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = BACKEND_DIR / "openapi" / "openapi.json"


def canonical_json_bytes(payload: Any) -> bytes:
    """Return stable, review-friendly JSON with no volatile metadata."""
    rendered = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"{rendered}\n".encode()


def canonical_openapi_bytes(application: FastAPI) -> bytes:
    """Render an application's OpenAPI schema in canonical form."""
    return canonical_json_bytes(application.openapi())


def _isolated_environment(root: Path) -> dict[str, str]:
    """Build an offline application environment rooted in a temporary directory."""
    storage = root / "storage"
    okf_bundle = root / "okf_bundle"
    storage.mkdir(mode=0o700)
    okf_bundle.mkdir(mode=0o700)
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "DOCVAULT_ENVIRONMENT": "test",
        "DOCVAULT_APP_NAME": "XENIUS DocVault",
        "DOCVAULT_DATABASE_URL": f"sqlite:///{(root / 'openapi.db').resolve()}",
        "DOCVAULT_DEBUG": "false",
        "DOCVAULT_ENABLE_DEMO_SEED": "false",
        "DOCVAULT_STORAGE_DIR": str(storage.resolve()),
        "DOCVAULT_OKF_BUNDLE_DIR": str(okf_bundle.resolve()),
        "DOCVAULT_SECRET_KEY": "openapi-export-isolation-only-not-production",
        "DOCVAULT_LLM_PROVIDER": "none",
        "DOCVAULT_ENABLE_EMBEDDINGS": "false",
        "DOCVAULT_USE_DOCLING": "false",
        "DOCVAULT_USE_QDRANT": "false",
        "DOCVAULT_EMBEDDING_MODEL": "",
        "DOCVAULT_RERANKER_MODEL": "",
        "DOCVAULT_ANTHROPIC_API_KEY": "",
        "DOCVAULT_VLLM_URL": "http://127.0.0.1:9/v1",
        "DOCVAULT_OLLAMA_URL": "http://127.0.0.1:9",
        "DOCVAULT_QDRANT_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "HF_TOKEN": "",
        "HUGGING_FACE_HUB_TOKEN": "",
    }


def render_isolated_openapi() -> bytes:
    """Import the application with temporary data paths and outbound providers off."""
    with TemporaryDirectory(prefix="docvault-openapi-") as temporary:
        environment = _isolated_environment(Path(temporary))
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        try:
            from app.main import app

            return canonical_openapi_bytes(app)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            temporary_path = Path(handle.name)
        temporary_path.chmod(0o644)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _diff(expected: bytes, current: bytes, snapshot: Path) -> str:
    return "".join(
        difflib.unified_diff(
            expected.decode("utf-8").splitlines(keepends=True),
            current.decode("utf-8").splitlines(keepends=True),
            fromfile=str(snapshot),
            tofile="generated OpenAPI contract",
        )
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or verify DocVault's canonical OpenAPI snapshot."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help=f"snapshot path (default: {DEFAULT_SNAPSHOT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail with a unified diff instead of updating the snapshot",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    snapshot = args.output.resolve()
    current = render_isolated_openapi()

    if args.check:
        if not snapshot.is_file():
            print(
                f"OpenAPI snapshot is missing: {snapshot}\n"
                "Generate it intentionally without --check.",
                file=sys.stderr,
            )
            return 2
        expected = snapshot.read_bytes()
        if expected != current:
            print(
                "OpenAPI contract drift detected. Review and classify the change "
                "before updating the snapshot.",
                file=sys.stderr,
            )
            print(_diff(expected, current, snapshot), file=sys.stderr, end="")
            return 1
        print(f"OpenAPI snapshot is current (sha256={_sha256(current)}).")
        return 0

    _write_atomically(snapshot, current)
    print(f"Wrote {snapshot} (sha256={_sha256(current)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
