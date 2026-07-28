"""Filesystem-backed object storage helpers."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from ..observability import trace_span
from ..storage import object_store


@dataclass(frozen=True)
class StoredFile:
    key: str
    path: Path


def resolve_storage_path(file_key: str) -> Path:
    """Resolve a database file key and ensure it remains inside storage."""
    root = object_store.root
    candidate = (root / file_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid storage key") from exc
    return candidate


def store_bytes(filename: str, data: bytes, checksum: str) -> StoredFile:
    with trace_span("object_storage", "promote"):
        staged = object_store.stage(io.BytesIO(data), suffix=Path(filename).suffix.lower())
        key = object_store.promote(staged.key, checksum=checksum)
        return StoredFile(key=key, path=resolve_storage_path(key))


def delete_file(file_key: str) -> bool:
    path = resolve_storage_path(file_key)
    if not path.exists():
        return False
    object_store.tombstone(file_key)
    return True
