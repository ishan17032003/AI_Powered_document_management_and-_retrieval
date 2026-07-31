"""Object storage helpers — backend-agnostic wrapper.

For the filesystem backend the ``file_key`` is a relative path inside the
storage root.  For the MinIO backend it is a composite ``<bucket>/<object>``
string; there is no local path and ``resolve_storage_path`` raises
``NotImplementedError`` when the MinIO adapter is active.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from ..observability import trace_span
from ..storage import object_store


@dataclass(frozen=True)
class StoredFile:
    key: str
    # ``path`` is only valid for the filesystem backend.  It is ``None`` when
    # the MinIO adapter is active so callers cannot accidentally rely on it.
    path: Path | None = None


def resolve_storage_path(file_key: str) -> Path:
    """Resolve a database file key to a filesystem path.

    Raises ``NotImplementedError`` when the MinIO backend is active — callers
    should use ``object_store.open(key)`` to stream the object instead.
    """
    from ..config import settings

    if settings.storage_backend != "filesystem":
        raise NotImplementedError(
            "resolve_storage_path is only available for the filesystem backend; "
            "use object_store.open(key) to stream MinIO objects."
        )
    root = object_store.root  # type: ignore[attr-defined]
    candidate = (root / file_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid storage key") from exc
    return candidate


def store_bytes(
    filename: str,
    data: bytes,
    checksum: str,
    *,
    class_name: str | None = None,
) -> StoredFile:
    """Stage, validate, and promote a document binary.

    ``class_name`` is forwarded to ``promote_to_class`` so the object lands in
    the correct MinIO bucket when the MinIO backend is active.  The filesystem
    backend ignores it.
    """
    with trace_span("object_storage", "promote"):
        staged = object_store.stage(
            io.BytesIO(data), suffix=Path(filename).suffix.lower()
        )
        key = object_store.promote_to_class(
            staged.key, checksum=checksum, class_name=class_name
        )
        from ..config import settings

        path: Path | None = None
        if settings.storage_backend == "filesystem":
            try:
                path = resolve_storage_path(key)
            except Exception:
                path = None
        return StoredFile(key=key, path=path)


def delete_file(file_key: str) -> bool:
    from ..config import settings

    if settings.storage_backend == "filesystem":
        try:
            path = resolve_storage_path(file_key)
        except Exception:
            return False
        if not path.exists():
            return False
    object_store.tombstone(file_key)
    return True
