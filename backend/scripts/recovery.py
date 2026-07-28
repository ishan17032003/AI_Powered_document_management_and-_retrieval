#!/usr/bin/env python3
"""Operator-facing non-destructive GC/backup/restore/DR commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT_PATH.parents[1]))

from app.services.recovery_service import (  # noqa: E402
    coordinated_backup,
    garbage_collection_candidates,
    rehearse_dr,
    restore_from_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="DocVault recovery operations")
    sub = parser.add_subparsers(dest="command", required=True)
    gc = sub.add_parser("gc-candidates")
    gc.add_argument("--database", type=Path, required=True)
    gc.add_argument("--storage", type=Path, required=True)
    gc.add_argument("--output", type=Path, required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--storage", type=Path, required=True)
    backup.add_argument("--okf", type=Path, required=True)
    backup.add_argument("--config", type=Path, help="configuration file or directory to include")
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--release-version", default="unknown")
    backup.add_argument("--schema-version", default="unknown")
    backup.add_argument("--quiescent-confirmed", action="store_true", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--alembic-ini", type=Path)
    dr = sub.add_parser("dr")
    dr.add_argument("--manifest", type=Path, required=True)
    dr.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "gc-candidates":
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        engine = create_engine(f"sqlite:///{args.database}")
        with Session(engine) as db:
            report = garbage_collection_candidates(db, args.storage)
    elif args.command == "backup":
        report = coordinated_backup(args.database, args.storage, args.okf, args.destination, config_root=args.config, release_version=args.release_version, schema_version=args.schema_version, quiescent_confirmed=args.quiescent_confirmed)
    elif args.command == "restore":
        report = restore_from_manifest(args.manifest, args.destination, alembic_ini=args.alembic_ini)
    else:
        report = rehearse_dr(args.manifest, args.workdir)
    output = args.output if args.command == "gc-candidates" else (args.destination / "verification.json" if args.command == "restore" else args.workdir / "dr-rehearsal.json" if args.command == "dr" else args.destination / "backup-result.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
