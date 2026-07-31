"""Authoritative binary ObjectStore boundary.

Two adapters are provided:
- FilesystemObjectStore   — local disk, original implementation (default).
- MinioObjectStore        — S3-compatible MinIO with per-class buckets.

The global ``object_store`` lazily resolves to the configured backend.
Callers never manipulate storage paths or bucket names directly; all key
semantics are encapsulated here.

Bucket naming for MinIO
-----------------------
Each document class gets its own bucket: ``<prefix>-<slug(class_name)>``.
Fixed infrastructure buckets:

  <prefix>-staging      — temporary objects before checksum validation
  <prefix>-unclassified — documents without an assigned class
  <prefix>-quarantine   — objects flagged by the virus/integrity scanner
  <prefix>-tombstone    — soft-deleted objects pending purge

The bucket slug is the class name lower-cased, with every run of
non-alphanumeric characters collapsed to a single hyphen.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


# ── Shared data classes ───────────────────────────────────────────────────────


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


# ── Bucket-naming helpers ─────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lower-case and collapse non-alphanumeric runs to a single hyphen."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "general"


def bucket_for_class(class_name: str | None, *, prefix: str = "docvault") -> str:
    """Return the MinIO bucket name for a given document class name.

    ``class_name=None`` maps to the *unclassified* bucket.

    >>> bucket_for_class("Invoice")
    'docvault-invoice'
    >>> bucket_for_class(None)
    'docvault-unclassified'
    """
    if not class_name:
        return f"{prefix}-unclassified"
    return f"{prefix}-{_slugify(class_name)}"


# ── Abstract interface ────────────────────────────────────────────────────────


class ObjectStore:
    """Minimal lifecycle contract for authoritative document binaries."""

    def stage(self, stream: BinaryIO, *, suffix: str = "") -> StagedObject:
        raise NotImplementedError

    def promote(self, staged_key: str, *, checksum: str) -> str:
        raise NotImplementedError

    def promote_to_class(
        self,
        staged_key: str,
        *,
        checksum: str,
        class_name: str | None = None,
    ) -> str:
        """Promote a staged object to its class-specific bucket.

        The default implementation falls back to ``promote`` (filesystem does
        not use classes).  MinioObjectStore overrides this to route to the
        correct class bucket.
        """
        return self.promote(staged_key, checksum=checksum)

    def move_to_class(self, key: str, new_class_name: str | None) -> str:
        """Move an existing object to a different class bucket.

        Returns the new key.  No-op by default (filesystem does not have
        class buckets).
        """
        return key

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


# ── Filesystem adapter (unchanged) ────────────────────────────────────────────


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


# ── MinIO adapter ─────────────────────────────────────────────────────────────


class MinioObjectStore(ObjectStore):
    """S3-compatible MinIO adapter with per-class bucket routing.

    Key format stored in DocVersion.file_key:
        ``<bucket>/<object_name>``

    For example: ``docvault-invoice/ab/abcdef1234...``

    Staged objects live in ``<prefix>-staging`` and are identified by a
    temporary UUID-based object name.
    """

    _CHUNK = 8 * 1024 * 1024  # 8 MB read chunks

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        secure: bool = False,
        bucket_prefix: str = "docvault",
    ) -> None:
        from minio import Minio  # type: ignore[import-untyped]

        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._prefix = bucket_prefix
        self._staging_bucket = f"{bucket_prefix}-staging"
        self._quarantine_bucket = f"{bucket_prefix}-quarantine"
        self._tombstone_bucket = f"{bucket_prefix}-tombstone"
        self._ensure_infrastructure_buckets()

    # ── Bucket lifecycle ──────────────────────────────────────────────────────

    def _ensure_bucket(self, name: str) -> None:
        if not self._client.bucket_exists(name):
            self._client.make_bucket(name)

    def _ensure_infrastructure_buckets(self) -> None:
        for bucket in (
            self._staging_bucket,
            self._quarantine_bucket,
            self._tombstone_bucket,
            f"{self._prefix}-unclassified",
        ):
            self._ensure_bucket(bucket)

    def ensure_class_bucket(self, class_name: str | None) -> str:
        """Ensure the class bucket exists and return its name."""
        name = bucket_for_class(class_name, prefix=self._prefix)
        self._ensure_bucket(name)
        return name

    # ── Key helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _split_key(key: str) -> tuple[str, str]:
        """Split ``bucket/object_name`` into ``(bucket, object_name)``."""
        slash = key.index("/")
        return key[:slash], key[slash + 1:]

    @staticmethod
    def _make_key(bucket: str, object_name: str) -> str:
        return f"{bucket}/{object_name}"

    # ── ObjectStore interface ─────────────────────────────────────────────────

    def stage(self, stream: BinaryIO, *, suffix: str = "") -> StagedObject:
        """Buffer the stream in a temp file, then upload to the staging bucket."""
        import uuid

        # Buffer locally so we can compute SHA-256 and get content_length.
        digest = hashlib.sha256()
        size = 0
        buf = io.BytesIO()
        while True:
            chunk = stream.read(self._CHUNK)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ValueError("object stream returned non-bytes")
            digest.update(chunk)
            size += len(chunk)
            buf.write(chunk)
        checksum = digest.hexdigest()
        buf.seek(0)

        object_name = f"stage-{uuid.uuid4().hex}{suffix}"
        self._client.put_object(
            self._staging_bucket,
            object_name,
            buf,
            length=size,
        )
        # Return a synthetic path for compatibility; callers must use the key.
        return StagedObject(
            key=self._make_key(self._staging_bucket, object_name),
            path=Path(f"/minio/{self._staging_bucket}/{object_name}"),
            checksum=checksum,
            size=size,
        )

    def promote(self, staged_key: str, *, checksum: str) -> str:
        """Promote staged object to the *unclassified* bucket."""
        return self.promote_to_class(staged_key, checksum=checksum, class_name=None)

    def promote_to_class(
        self,
        staged_key: str,
        *,
        checksum: str,
        class_name: str | None = None,
    ) -> str:
        """Promote staged object to the correct class bucket."""
        from minio.commonconfig import CopySource  # type: ignore[import-untyped]

        if not checksum or len(checksum) != 64:
            raise ValueError("invalid checksum")

        staging_bucket, stage_object = self._split_key(staged_key)

        # Verify the staged object exists.
        try:
            self._client.stat_object(staging_bucket, stage_object)
        except Exception as exc:
            raise FileNotFoundError("staged object not found") from exc

        # Route to the correct class bucket.
        dest_bucket = self.ensure_class_bucket(class_name)
        dest_object = f"{checksum[:2]}/{checksum}"

        # Copy to destination (idempotent).
        self._client.copy_object(
            dest_bucket,
            dest_object,
            CopySource(staging_bucket, stage_object),
        )
        # Remove staging copy.
        try:
            self._client.remove_object(staging_bucket, stage_object)
        except Exception:
            pass  # Non-fatal; staging objects are eventually cleaned up.

        return self._make_key(dest_bucket, dest_object)

    def move_to_class(self, key: str, new_class_name: str | None) -> str:
        """Copy an object to a different class bucket and remove the original.

        Returns the new composite key ``<bucket>/<object>``.
        """
        from minio.commonconfig import CopySource  # type: ignore[import-untyped]

        src_bucket, src_object = self._split_key(key)
        dest_bucket = self.ensure_class_bucket(new_class_name)

        if dest_bucket == src_bucket:
            return key  # Already in the right bucket.

        self._client.copy_object(
            dest_bucket,
            src_object,
            CopySource(src_bucket, src_object),
        )
        try:
            self._client.remove_object(src_bucket, src_object)
        except Exception:
            pass  # Leave orphan; object is already accessible from dest.

        return self._make_key(dest_bucket, src_object)

    def open(self, key: str) -> BinaryIO:
        """Download *key* from MinIO into a temp file and return it.

        The caller receives a real file handle with a ``.name`` attribute so
        that code like ``Path(handle.name)`` works identically whether the
        backend is local-disk or MinIO.  The temp file is deleted when the
        handle is closed (delete=True is the default on all POSIX platforms).
        """
        import tempfile

        bucket, object_name = self._split_key(key)
        response = self._client.get_object(bucket, object_name)
        try:
            # Determine file suffix from the object name for libraries that
            # inspect the file extension (e.g. Docling, PyMuPDF).
            suffix = Path(object_name).suffix or ""
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=True)
            try:
                for chunk in response.stream(amt=1 << 16):  # 64 KiB chunks
                    tmp.write(chunk)
                tmp.flush()
                tmp.seek(0)
            except Exception:
                tmp.close()
                raise
        finally:
            response.close()
            response.release_conn()
        return tmp  # caller is responsible for closing (and therefore deleting)

    def stat(self, key: str) -> ObjectStat:
        bucket, object_name = self._split_key(key)
        info = self._client.stat_object(bucket, object_name)
        # MinIO stores the SHA-256 in the etag for objects uploaded via put_object.
        checksum = (info.etag or "").strip('"')
        return ObjectStat(
            key=key,
            size=info.size or 0,
            checksum=checksum,
            state="AVAILABLE",
        )

    def verify(self, key: str, *, checksum: str) -> bool:
        try:
            s = self.stat(key)
            # If etag is the sha256, compare directly; otherwise stream & hash.
            if len(s.checksum) == 64:
                return s.checksum == checksum
            # Fallback: re-download and hash.
            data = self.open(key).read()
            return hashlib.sha256(data).hexdigest() == checksum
        except Exception:
            return False

    def _move_to_bucket(self, key: str, dest_bucket: str) -> str:
        from minio.commonconfig import CopySource  # type: ignore[import-untyped]

        src_bucket, src_object = self._split_key(key)
        self._client.copy_object(
            dest_bucket,
            src_object,
            CopySource(src_bucket, src_object),
        )
        try:
            self._client.remove_object(src_bucket, src_object)
        except Exception:
            pass
        return self._make_key(dest_bucket, src_object)

    def quarantine(self, key: str) -> str:
        return self._move_to_bucket(key, self._quarantine_bucket)

    def tombstone(self, key: str) -> str:
        return self._move_to_bucket(key, self._tombstone_bucket)


# ── Lazy global store ─────────────────────────────────────────────────────────


class _LazyObjectStore(ObjectStore):
    """Avoid loading application settings for pure storage contract tests."""

    _instance: FilesystemObjectStore | MinioObjectStore | None = None

    def _resolved(self) -> FilesystemObjectStore | MinioObjectStore:
        if self._instance is None:
            from .config import settings

            if settings.storage_backend == "minio":
                self._instance = MinioObjectStore(
                    endpoint=settings.minio_endpoint,
                    access_key=settings.minio_access_key,
                    secret_key=settings.minio_secret_key,
                    secure=settings.minio_secure,
                    bucket_prefix=settings.minio_bucket_prefix,
                )
            else:
                self._instance = FilesystemObjectStore(settings.storage_dir)
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._resolved(), name)

    def stage(self, stream: BinaryIO, *, suffix: str = "") -> StagedObject:
        return self._resolved().stage(stream, suffix=suffix)

    def promote(self, staged_key: str, *, checksum: str) -> str:
        return self._resolved().promote(staged_key, checksum=checksum)

    def promote_to_class(
        self,
        staged_key: str,
        *,
        checksum: str,
        class_name: str | None = None,
    ) -> str:
        return self._resolved().promote_to_class(
            staged_key, checksum=checksum, class_name=class_name
        )

    def move_to_class(self, key: str, new_class_name: str | None) -> str:
        return self._resolved().move_to_class(key, new_class_name)

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
