"""Read-only reconciliation of authoritative rows, objects, and FTS data.

The relational database is authoritative.  This module never mutates either
the database or object storage and returns a bounded, deterministic report that
can be used by operators before any separately-reviewed repair command.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..retrieval_store import RetrievalStore
from ..repositories.search_repository import qdrant_health_status

_CATEGORIES = ("missing_object", "orphan_object", "checksum_mismatch", "missing_index", "stale_index")


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reconcile(
    db: Session,
    storage_root: Path,
    *,
    max_items: int = 10_000,
    retrieval_stores: tuple[RetrievalStore, ...] = (),
) -> dict[str, Any]:
    """Return a bounded drift report without changing state.

    ``storage_root`` is explicit so scheduled jobs and tests cannot silently
    reconcile an unintended directory.  Only immutable ``objects/`` keys are
    considered; staging/quarantine/tombstone areas are lifecycle state, not
    orphaned authoritative objects.
    """
    if type(max_items) is not int or not 1 <= max_items <= 100_000:
        raise ValueError("max_items must be between 1 and 100000")
    root = Path(storage_root).resolve()
    categories: dict[str, list[dict[str, Any]]] = {key: [] for key in _CATEGORIES}
    examined = 0

    versions = db.query(models.DocVersion).filter(models.DocVersion.storage_state != "DELETED").all()
    referenced: dict[str, tuple[models.DocVersion, Path]] = {}
    current_doc_ids: set[int] = set()
    for version in versions:
        if version.document and version.document.lifecycle_state == "ACTIVE":
            current_doc_ids.add(version.document_id)
        path = (root / version.file_key).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            categories["missing_object"].append({"document_id": version.document_id, "version_id": version.id, "reason": "invalid_key"})
            continue
        referenced[version.file_key] = (version, path)
        if not path.is_file():
            categories["missing_object"].append({"document_id": version.document_id, "version_id": version.id, "file_key": version.file_key})
        elif version.storage_state == "AVAILABLE":
            actual = _checksum(path)
            if actual != version.checksum:
                categories["checksum_mismatch"].append({"document_id": version.document_id, "version_id": version.id, "file_key": version.file_key, "expected": version.checksum, "actual": actual})

    objects_root = root / "objects"
    if objects_root.is_dir():
        for path in sorted(p for p in objects_root.rglob("*") if p.is_file()):
            examined += 1
            key = str(path.relative_to(root))
            if key not in referenced and len(categories["orphan_object"]) < max_items:
                categories["orphan_object"].append({"file_key": key, "size": path.stat().st_size})
            if examined >= max_items:
                break

    try:
        rows = db.execute(text("SELECT document_id FROM doc_fts")).scalars().all()
    except Exception:
        rows = []
    indexed_ids = {int(value) for value in rows if isinstance(value, int)}
    for doc_id in sorted(current_doc_ids - indexed_ids):
        if len(categories["missing_index"]) < max_items:
            categories["missing_index"].append({"document_id": doc_id})
    for doc_id in sorted(indexed_ids - current_doc_ids):
        if len(categories["stale_index"]) < max_items:
            categories["stale_index"].append({"document_id": doc_id})

    counts = {key: len(value) for key, value in categories.items()}
    authoritative_versions: dict[int, str | int | None] = {}
    for document in db.query(models.Document).filter(models.Document.lifecycle_state == "ACTIVE"):
        versions = [version for version in document.versions if version.storage_state != "DELETED"]
        if versions:
            latest = max(versions, key=lambda version: version.version_no)
            authoritative_versions[document.id] = latest.id
    retrieval_reports: dict[str, dict[str, Any]] = {}
    for store in retrieval_stores:
        report = store.reconcile(authoritative_versions, max_items=max_items)
        retrieval_reports[report.adapter] = {
            "checked": report.checked,
            "indexed_document_ids": sorted(report.indexed_document_ids),
            "missing_document_ids": sorted(report.missing_document_ids),
            "stale_document_ids": sorted(report.stale_document_ids),
            "version_lag_document_ids": sorted(report.version_lag_document_ids),
            "detail": report.detail,
        }
    retrieval_drift = sum(
        len(report["missing_document_ids"])
        + len(report["stale_document_ids"])
        + len(report["version_lag_document_ids"])
        for report in retrieval_reports.values()
    )
    return {
        "schema_version": "reconciliation-v1",
        "read_only": True,
        "bounded": examined >= max_items,
        "retrieval_adapters": {
            "fts5": {"checked": True, "rows": len(indexed_ids)},
            # Qdrant is optional; report only its bounded cached state and
            # never force a network call during a read-only reconciliation.
            "qdrant": {
                "checked": False,
                "state": qdrant_health_status().state,
            },
        },
        "summary": counts,
        "total_drift": sum(counts.values()) + retrieval_drift,
        "items": categories,
        "retrieval_stores": retrieval_reports,
    }
