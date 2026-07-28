#!/usr/bin/env python3
"""Plan a narrowly scoped, reviewed storage repair.

The command is deliberately fail-closed: targets are explicit paths relative to
the storage root, a backup identifier is mandatory, and mutation requires both
``--execute`` and an approval token.  The normal (and CI-safe) operation is a
dry-run that emits an auditable JSON plan and never changes user data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Permit ``python backend/scripts/reviewed_repair.py`` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.reviewed_repair import RepairError, build_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--actor", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-token", help="required for --execute; use a reviewed token")
    parser.add_argument("--audit-log", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.execute and (not args.approval_token or len(args.approval_token) < 16):
        raise RepairError("--execute requires an approval token of at least 16 characters")
    plan = build_plan(
        storage=args.storage,
        targets=args.target,
        backup_id=args.backup_id,
        actor=args.actor,
        dry_run=args.dry_run,
    )
    encoded = json.dumps(plan, sort_keys=True)
    print(encoded)
    if args.audit_log:
        args.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with args.audit_log.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    # No destructive mutation is performed by this bounded command. A reviewed
    # executor can consume the plan and quarantine targets separately.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepairError as exc:
        raise SystemExit(f"repair refused: {exc}") from None
