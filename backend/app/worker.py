"""Dedicated ingestion worker process entrypoint.

Run one process for SQLite. PostgreSQL deployments may run multiple copies
after D-02 approval; repository claims use row locks and SKIP LOCKED there.

Concurrency model
-----------------
Each ``ingestion_worker_count`` slot runs as an independent thread.  Every slot
maintains its own SQLAlchemy session and calls ``run_next_job`` in a tight loop,
sleeping ``poll_seconds`` between polls.  Because job claims use
``SELECT … FOR UPDATE SKIP LOCKED`` on PostgreSQL, two slots never race for the
same job row.  On SQLite a module-level ``threading.Lock`` serialises only the
claim step (not the expensive extraction work) so both slots can process
different documents simultaneously.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

os.makedirs("/data/tmp", exist_ok=True)
tempfile.tempdir = "/data/tmp"

from .bootstrap import prepare_runtime_directories
from .database import SessionLocal
from .model_prefetch import prefetch_models
from .model_warmup import warm_visual_semantic
from .schema_compatibility import assert_schema_compatible
from .runtime import settings
from .services.ingestion_worker import run_next_job

# SQLite does not support SKIP LOCKED; serialise the claim step only.
_sqlite_claim_lock: threading.Lock = threading.Lock()
_is_sqlite: bool | None = None   # resolved once in run()


def _use_claim_lock() -> bool:
    global _is_sqlite
    if _is_sqlite is None:
        _is_sqlite = "sqlite" in str(settings.database_url).lower()
    return bool(_is_sqlite)


def _job_slot(
    *,
    owner: str,
    poll_seconds: float,
    shutdown: threading.Event,
) -> None:
    """One independent job-claiming loop.  Runs in its own thread."""
    log = logging.getLogger(__name__)
    while not shutdown.is_set():
        db = SessionLocal()
        try:
            if _use_claim_lock():
                with _sqlite_claim_lock:
                    job = run_next_job(db, owner=owner)
            else:
                job = run_next_job(db, owner=owner)
            if job is not None:
                log.info("ingestion job %s -> %s", job.id, job.state)
        except Exception as exc:
            log.error("Job slot %s error: %s", owner, exc, exc_info=True)
        finally:
            db.close()
        # Wait for poll_seconds or until shutdown is requested.
        shutdown.wait(timeout=poll_seconds)


def run(*, once: bool = False, poll_seconds: float = 1.0) -> int:
    assert_schema_compatible(settings.database_url)
    prepare_runtime_directories(settings)
    # Download embedding + reranker models into the host-mapped hf_cache
    # directory on first boot. No-op on subsequent starts when already cached.
    prefetch_models(
        embedding_model=settings.embedding_model,
        reranker_model=settings.reranker_model,
    )
    # Warm the extraction/ingestion models on background threads so the first
    # claimed job does not pay the Docling/SigLIP2 cold-start.
    from .services.extraction_service import warm_docling

    warm_docling()
    warm_visual_semantic()

    base_owner = f"ingestion-worker-{os.getpid()}-{uuid4().hex[:8]}"
    slots = max(1, settings.ingestion_worker_count)
    shutdown = threading.Event()

    def _handle_signal(sig: int, frame: object) -> None:  # noqa: ARG001
        logging.getLogger(__name__).info(
            "Worker received signal %s — shutting down after current jobs finish", sig
        )
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if once:
        # --once: run a single poll across all slots then exit.
        shutdown_after_one = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=slots) as pool:
            futs = [
                pool.submit(
                    _job_slot,
                    owner=f"{base_owner}-slot{i}",
                    poll_seconds=0,
                    shutdown=shutdown_after_one,
                )
                for i in range(slots)
            ]
            shutdown_after_one.set()          # wake all slots immediately
            concurrent.futures.wait(futs)
        return 0

    logging.getLogger(__name__).info(
        "Worker started: %d slot(s), poll_interval=%.1fs", slots, poll_seconds
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=slots) as pool:
        futs = [
            pool.submit(
                _job_slot,
                owner=f"{base_owner}-slot{i}",
                poll_seconds=poll_seconds,
                shutdown=shutdown,
            )
            for i in range(slots)
        ]
        concurrent.futures.wait(futs)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="DocVault durable ingestion worker")
    parser.add_argument("--once", action="store_true", help="process at most one job per slot")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.poll_seconds < 0.1 or args.poll_seconds > 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    return run(once=args.once, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
