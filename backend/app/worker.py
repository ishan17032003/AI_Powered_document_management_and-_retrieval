"""Dedicated ingestion worker process entrypoint.

Run one process for SQLite. PostgreSQL deployments may run multiple copies
after D-02 approval; repository claims use row locks and SKIP LOCKED there.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from uuid import uuid4

from .bootstrap import prepare_runtime_directories
from .database import SessionLocal
from .schema_compatibility import assert_schema_compatible
from .runtime import settings
from .services.ingestion_worker import run_next_job


def run(*, once: bool = False, poll_seconds: float = 1.0) -> int:
    assert_schema_compatible(settings.database_url)
    prepare_runtime_directories(settings)
    owner = f"ingestion-worker-{os.getpid()}-{uuid4().hex[:8]}"
    while True:
        db = SessionLocal()
        try:
            job = run_next_job(db, owner=owner)
            if job is not None:
                logging.getLogger(__name__).info("ingestion job %s -> %s", job.id, job.state)
        finally:
            db.close()
        if once:
            return 0
        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="DocVault durable ingestion worker")
    parser.add_argument("--once", action="store_true", help="process at most one job")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.poll_seconds < 0.1 or args.poll_seconds > 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    return run(once=args.once, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
