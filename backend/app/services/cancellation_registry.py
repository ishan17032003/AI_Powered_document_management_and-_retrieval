"""Process-shared cancellation registry for in-flight extraction jobs.

Workers poll ``is_cancelled(document_id)`` between batch submissions.
``cancel(document_id)`` is called by ``delete_document()`` as soon as the
tombstone is committed; the worker sees the flag on its next poll and aborts.

The registry is backed by a ``multiprocessing.Manager().dict()`` so the flag
is visible across the main thread, all ``ProcessPoolExecutor`` subprocess
workers, and all ingestion thread slots.  The manager process is started lazily
on first use and lives for the lifetime of the worker process.
"""

from __future__ import annotations

import logging
import multiprocessing
import multiprocessing.managers
import threading
from typing import Optional

_log = logging.getLogger(__name__)

# Module-level singletons — initialised once in the worker process.
_manager: Optional[multiprocessing.managers.SyncManager] = None
_registry: Optional[dict] = None          # Manager().dict()  {doc_id: bool}
_init_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_started() -> dict:
    """Return the shared registry dict, starting the manager on first call."""
    global _manager, _registry
    if _registry is not None:
        return _registry
    with _init_lock:
        if _registry is not None:          # double-check after acquiring lock
            return _registry
        try:
            mgr = multiprocessing.Manager()
            reg = mgr.dict()
            _manager = mgr
            _registry = reg
            _log.info(
                "cancellation_registry: manager started (pid=%d)",
                mgr._process.pid,  # type: ignore[attr-defined]
            )
        except Exception as exc:
            # Non-fatal: fall back to an in-process plain dict.  Cancel signals
            # will still reach the main worker thread but not subprocess workers.
            _log.warning(
                "cancellation_registry: Manager() failed (%s); using in-process dict "
                "— subprocess cancellation will not propagate",
                exc,
            )
            _registry = {}  # type: ignore[assignment]
    return _registry  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_registry() -> dict:
    """Return the shared cancellation dict for use as a pool initializer arg."""
    return _ensure_started()


def register(document_id: int) -> None:
    """Mark a document as active (not cancelled).  Call before extraction starts."""
    try:
        _ensure_started()[document_id] = False
    except Exception as exc:
        _log.warning("cancellation_registry.register(%s) failed: %s", document_id, exc)


def cancel(document_id: int) -> None:
    """Signal all workers processing *document_id* to abort.

    Idempotent — safe to call even if the document was never registered or has
    already been unregistered.
    """
    try:
        _ensure_started()[document_id] = True
        _log.info(
            "cancellation_registry: document %s marked for cancellation", document_id
        )
    except Exception as exc:
        _log.warning("cancellation_registry.cancel(%s) failed: %s", document_id, exc)


def is_cancelled(document_id: int, registry: dict | None = None) -> bool:
    """Return True if *document_id* has been cancelled.

    Checks both the local shared memory registry and the shared PostgreSQL
    database so deletions from the backend container are immediately visible
    to the ingestion-worker container.
    """
    try:
        r = registry if registry is not None else _ensure_started()
        if bool(r.get(document_id, False)):
            return True
    except Exception:
        pass

    # Cross-container DB check (PostgreSQL is shared between backend and worker containers)
    try:
        from ..database import SessionLocal
        from ..models import Document, IngestionJob

        with SessionLocal() as db:
            doc = (
                db.query(Document.deleted_at, Document.failure_code)
                .filter(Document.id == document_id)
                .first()
            )
            if doc is not None and (doc.deleted_at is not None or doc.failure_code == "TOMBSTONED"):
                return True
            job = (
                db.query(IngestionJob.state)
                .filter(
                    IngestionJob.document_id == document_id,
                    IngestionJob.state == "CANCELLED",
                )
                .first()
            )
            if job is not None:
                return True
    except Exception:
        pass
    return False


def unregister(document_id: int) -> None:
    """Remove *document_id* from the registry once processing is complete."""
    try:
        _ensure_started().pop(document_id, None)
    except Exception as exc:
        _log.warning("cancellation_registry.unregister(%s) failed: %s", document_id, exc)
