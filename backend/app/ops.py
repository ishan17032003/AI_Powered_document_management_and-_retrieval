"""Bounded deployment process entrypoints.

Each subcommand owns one operational responsibility; compose runs these as
separate one-shot services rather than combining migrations/backup work with
HTTP or ingestion workers.
"""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def _sqlite_path(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise SystemExit("backup process currently requires a SQLite database path")
    return Path(url.removeprefix("sqlite:///" ).split("?", 1)[0]).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DocVault bounded process roles")
    parser.add_argument("role", choices=("web", "worker", "migrate", "maintenance", "backup"))
    args = parser.parse_args(argv)
    if args.role == "migrate":
        return subprocess.call(["alembic", "upgrade", "head"])
    if args.role == "worker":
        from .worker import main as worker_main
        return worker_main()
    if args.role == "maintenance":
        # Read-only compatibility/reconciliation check; adapter optimize jobs
        # remain explicitly scheduled outside request and worker processes.
        from .schema_compatibility import main as compatibility_main
        return compatibility_main()
    if args.role == "backup":
        destination = os.environ.get("DOCVAULT_BACKUP_DESTINATION", "").strip()
        if not destination or os.environ.get("DOCVAULT_BACKUP_QUIESCENT") != "true":
            raise SystemExit("backup requires DOCVAULT_BACKUP_DESTINATION and DOCVAULT_BACKUP_QUIESCENT=true")
        from .config import settings
        from .services.recovery_service import coordinated_backup
        coordinated_backup(
            _sqlite_path(settings.database_url),
            settings.storage_dir,
            settings.okf_bundle_dir,
            Path(destination),
            quiescent_confirmed=True,
            release_version=os.environ.get("DOCVAULT_RELEASE_VERSION", "unknown"),
        )
        return 0
    from .config import settings
    workers = os.environ.get("GUNICORN_WORKERS", "2")
    return subprocess.call([
        "gunicorn", "app.main:app", "--worker-class", "uvicorn.workers.UvicornWorker",
        "--workers", workers, "--bind", "0.0.0.0:8000", "--timeout", "300",
        "--graceful-timeout", "30", "--error-logfile", "-", "--log-level", "info",
    ])


if __name__ == "__main__":
    raise SystemExit(main())
