"""Authoritative binary ObjectStore boundary.

The local adapter implements D-03's appliance profile. A shared deployment must
provide an encrypted object-storage adapter behind this same interface; callers
never manipulate storage paths directly.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO



@dataclass(frozen=True, slots=True)
class ObjectStat:
    key: str
    size: int
    checksum: str
    state: str


@dataclass(frozen=True, slots=True)
class StagedObject:
    key: str
    path: Path
    checksum: str
    size: int


class ObjectStore:
    """Minimal lifecycle contract for authoritative document binaries."""

    def stage(self, stream: BinaryIO, *, suffix: str = "") -> StagedObject:
        raise NotImplementedError

    def promote(self, staged_key: str, *, checksum: str) -> str:
        raise NotImplementedError

    def open(self, key: str) -> BinaryIO:
        raise NotImplementedError

    def stat(self, key: str) -> ObjectStat:
        raise NotImplementedError

    def verify(self, key: str, *, checksum: str) -> bool:
        raise NotImplementedError

    def quarantine(self, key: str) -> str:
        raise NotImplementedError

    def tombstone(self, key: str) -> str:
        raise NotImplementedError


class FilesystemObjectStore(ObjectStore):
    """Atomic, checksum-addressed local implementation."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.staging_root = self.root / ".staging"
        self.quarantine_root = self.root / ".quarantine"
        self.tombstone_root = self.root / ".tombstone"

    def _safe(self, key: str) -> Path:
        if not isinstance(key, str) or not key or "\x00" in key:
            raise ValueError("invalid object key")
        candidate = (self.root / key).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("invalid object key") from exc
        return candidate

    def stage(self, stream: BinaryIO, *, suffix: str = "") -> StagedObject:
        self.staging_root.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="stage-", suffix=suffix, dir=self.staging_root)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as destination:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ValueError("object stream returned non-bytes")
                    digest.update(chunk)
                    size += len(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except Exception:
            Path(raw_path).unlink(missing_ok=True)
            raise
        return StagedObject(
            key=f".staging/{Path(raw_path).name}",
            path=Path(raw_path),
            checksum=digest.hexdigest(),
            size=size,
        )

    def promote(self, staged_key: str, *, checksum: str) -> str:
        staged = self._safe(staged_key)
        if not checksum or len(checksum) != 64:
            raise ValueError("invalid checksum")
        if not staged.is_file():
            raise FileNotFoundError("staged object not found")
        staged_digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        if staged_digest != checksum:
            raise IOError("staged object checksum mismatch")
        final_key = f"objects/{checksum[:2]}/{checksum}"
        destination = self._safe(final_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not self.verify(final_key, checksum=checksum):
                raise IOError("immutable object checksum conflict")
            staged.unlink(missing_ok=True)
            return final_key
        os.replace(staged, destination)
        return final_key

    def open(self, key: str) -> BinaryIO:
        return self._safe(key).open("rb")

    def stat(self, key: str) -> ObjectStat:
        path = self._safe(key)
        stat = path.stat()
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        return ObjectStat(key=key, size=stat.st_size, checksum=checksum, state="AVAILABLE")

    def verify(self, key: str, *, checksum: str) -> bool:
        try:
            return self.stat(key).checksum == checksum
        except (FileNotFoundError, OSError, ValueError):
            return False

    def _move_state(self, key: str, root: Path, state: str) -> str:
        source = self._safe(key)
        if not source.exists():
            return f".{state}/{key}"
        destination = (root / key).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        return str(destination.relative_to(self.root))

    def quarantine(self, key: str) -> str:
        return self._move_state(key, self.quarantine_root, "quarantine")

    def tombstone(self, key: str) -> str:
        return self._move_state(key, self.tombstone_root, "tombstone")


class _LazyObjectStore(ObjectStore):
    """Avoid loading application settings for pure storage contract tests."""

    _instance: FilesystemObjectStore | None = None

    def _resolved(self) -> FilesystemObjectStore:
        if self._instance is None:
            from .config import settings

            self._instance = FilesystemObjectStore(settings.storage_dir)
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._resolved(), name)

    # These names exist on ObjectStore, so inherited NotImplemented methods
    # would otherwise bypass __getattr__ and never resolve the adapter.
    def stage(self, stream: BinaryIO, *, suffix: str = "") -> StagedObject:
        return self._resolved().stage(stream, suffix=suffix)

    def promote(self, staged_key: str, *, checksum: str) -> str:
        return self._resolved().promote(staged_key, checksum=checksum)

    def open(self, key: str) -> BinaryIO:
        return self._resolved().open(key)

    def stat(self, key: str) -> ObjectStat:
        return self._resolved().stat(key)

    def verify(self, key: str, *, checksum: str) -> bool:
        return self._resolved().verify(key, checksum=checksum)

    def quarantine(self, key: str) -> str:
        return self._resolved().quarantine(key)

    def tombstone(self, key: str) -> str:
        return self._resolved().tombstone(key)


object_store = _LazyObjectStore()


def immutable_key(checksum: str) -> str:
    if not isinstance(checksum, str) or len(checksum) != 64:
        raise ValueError("invalid checksum")
    return f"objects/{checksum[:2]}/{checksum}"
