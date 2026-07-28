"""Fail-closed planning primitives for reviewed storage repairs."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RepairError(ValueError):
    """Raised when a repair request is unsafe or incomplete."""


def build_plan(*, storage: Path, targets: list[str], backup_id: str, actor: str, dry_run: bool) -> dict[str, Any]:
    if not backup_id.strip() or len(backup_id) > 128:
        raise RepairError("backup_id is required and must be at most 128 characters")
    if not actor.strip() or len(actor) > 128:
        raise RepairError("actor is required and must be at most 128 characters")
    if not targets:
        raise RepairError("at least one explicit --target is required")
    root = storage.resolve()
    resolved: list[dict[str, Any]] = []
    for raw in targets:
        candidate = (root / raw).resolve()
        if not candidate.is_relative_to(root):
            raise RepairError(f"target escapes storage root: {raw}")
        if candidate == root or not candidate.exists() or not candidate.is_file():
            raise RepairError(f"target must be an existing file: {raw}")
        resolved.append({"target": candidate.relative_to(root).as_posix(), "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(), "bytes": candidate.stat().st_size})
    return {"event": "storage.repair.plan", "created_at": datetime.now(timezone.utc).isoformat(), "actor": actor, "backup_id": backup_id, "dry_run": dry_run, "targets": resolved, "mutation": "none" if dry_run else "quarantine-only-after-approval"}
