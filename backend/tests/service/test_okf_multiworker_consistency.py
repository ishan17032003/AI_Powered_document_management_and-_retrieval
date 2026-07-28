"""Cross-process cache consistency for shared OKF knowledge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]

_WORKER = r"""
import json
import sys

from app.services import okf_service

for line in sys.stdin:
    command = json.loads(line)
    operation = command["operation"]
    if operation == "get":
        result = [entry.title for entry in okf_service.get_bundle()]
    elif operation == "save":
        result = okf_service.create_entry(command["filename"], command["content"])
    elif operation == "reload":
        result = okf_service.reload_bundle()
    elif operation == "quit":
        print(json.dumps({"ok": True}), flush=True)
        break
    else:
        raise RuntimeError("unsupported test operation")
    print(json.dumps(result), flush=True)
"""


def _start_worker(bundle_dir: Path) -> subprocess.Popen[str]:
    environment = {
        **os.environ,
        "DOCVAULT_ENVIRONMENT": "test",
        "DOCVAULT_OKF_BUNDLE_DIR": str(bundle_dir),
    }
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _WORKER],
        cwd=BACKEND_DIR,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _command(
    process: subprocess.Popen[str],
    operation: str,
    **payload: Any,
) -> Any:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps({"operation": operation, **payload}) + "\n")
    process.stdin.flush()
    response = process.stdout.readline()
    if not response:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"OKF worker exited without a response: {stderr}")
    return json.loads(response)


def _stop_worker(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        _command(process, "quit")
        process.wait(timeout=5)
    except (AssertionError, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=5)


def test_replacement_and_revocation_invalidate_another_worker(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    entry_path = bundle_dir / "shared.md"
    entry_path.write_text(
        "---\ntitle: Version One\n---\nInitial knowledge.",
        encoding="utf-8",
    )

    first_worker = _start_worker(bundle_dir)
    second_worker = _start_worker(bundle_dir)
    try:
        assert _command(first_worker, "get") == ["Version One"]
        assert _command(second_worker, "get") == ["Version One"]

        saved = _command(
            first_worker,
            "save",
            filename="shared.md",
            content="---\ntitle: Version Two\n---\nReplacement knowledge.",
        )
        assert saved["title"] == "Version Two"
        assert _command(second_worker, "get") == ["Version Two"]

        entry_path.unlink()
        assert _command(first_worker, "reload") == 0
        assert _command(second_worker, "get") == []
    finally:
        _stop_worker(first_worker)
        _stop_worker(second_worker)


@pytest.mark.parametrize("operation", ["replace", "revoke"])
def test_concurrent_change_during_load_never_pins_stale_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from app.repositories import okf_repository
    from app.services import okf_service

    bundle_dir = tmp_path / "race-bundle"
    bundle_dir.mkdir()
    entry_path = bundle_dir / "shared.md"
    entry_path.write_text(
        "---\ntitle: Version One\n---\nInitial knowledge.",
        encoding="utf-8",
    )
    monkeypatch.setattr(okf_service.settings, "okf_bundle_dir", bundle_dir)
    monkeypatch.setattr(okf_service, "_bundle", None)
    monkeypatch.setattr(okf_service, "_bundle_revision", None)

    original_load = okf_repository.load_bundle
    first_read_complete = threading.Event()
    allow_first_read_to_return = threading.Event()
    load_count = 0

    def barrier_load(path: Path):
        nonlocal load_count
        entries = original_load(path)
        load_count += 1
        if load_count == 1:
            first_read_complete.set()
            assert allow_first_read_to_return.wait(timeout=5)
        return entries

    monkeypatch.setattr(okf_repository, "load_bundle", barrier_load)
    observed: list[str] = []

    def read_bundle() -> None:
        observed.extend(entry.title for entry in okf_service.get_bundle())

    reader = threading.Thread(target=read_bundle)
    reader.start()
    assert first_read_complete.wait(timeout=5)
    if operation == "replace":
        okf_repository.save_entry(
            bundle_dir,
            "shared.md",
            "---\ntitle: Version Two\n---\nReplacement knowledge.",
        )
    else:
        entry_path.unlink()
    allow_first_read_to_return.set()
    reader.join(timeout=5)

    assert not reader.is_alive()
    assert load_count >= 2
    assert observed == (["Version Two"] if operation == "replace" else [])
