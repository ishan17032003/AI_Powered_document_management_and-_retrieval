"""Fail-closed visual validation, deterministic lineage, and lifecycle rules."""

from __future__ import annotations

import hashlib
import multiprocessing
import resource
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from time import monotonic

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..storage import immutable_key

_VISUAL_TYPES = {"image/png", "image/jpeg", "image/gif", "image/tiff", "image/webp"}
_STAGES = ("VALIDATE", "EXTRACT", "DERIVE", "PERSIST")
_LIFECYCLE_TRANSITIONS = {
    "ACTIVE": {"SUPERSEDED", "TOMBSTONED", "QUARANTINED", "DELETED"},
    "SUPERSEDED": {"TOMBSTONED", "DELETED"},
    "QUARANTINED": {"DELETED"},
    "TOMBSTONED": {"DELETED"},
    "DELETED": set(),
}


def _reject_trailing_polyglot(data: bytes, content_type: str) -> None:
    """Reject bytes after a complete container terminator."""
    if content_type == "image/png":
        # IEND is a fixed 12-byte PNG chunk and must be the final bytes.
        if len(data) < 12 or data[-8:-4] != b"IEND":
            raise VisualValidationError("visual payload has trailing data")
    elif content_type == "image/jpeg":
        if not data.rstrip().endswith(b"\xff\xd9"):
            raise VisualValidationError("visual payload has trailing data")
    elif content_type == "image/gif":
        if not data.rstrip().endswith(b";"):
            raise VisualValidationError("visual payload has trailing data")
    elif content_type == "image/webp":
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise VisualValidationError("visual payload container is invalid")
        declared_size = int.from_bytes(data[4:8], "little") + 8
        if declared_size != len(data):
            raise VisualValidationError("visual payload has trailing data")


class VisualValidationError(ValueError):
    code = "VISUAL_VALIDATION_FAILED"


def _address_space_limit(memory_limit_mb: int) -> int:
    """Bound additional child address space without rejecting large runtimes.

    ``RLIMIT_AS`` is an absolute virtual-address limit.  The worker imports
    the application before entering this function, and optional ML packages
    can reserve more than 512 MB before decoding a single image.  Preserve
    the configured budget as additional space above that baseline while still
    applying a finite cap to the isolated child.
    """

    requested = memory_limit_mb * 1024 * 1024
    baseline = 0
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmSize:"):
                baseline = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        baseline = 0
    return min(max(requested, baseline + requested), 8 * 1024 * 1024 * 1024)


@dataclass(frozen=True, slots=True)
class EmbeddedRegionLineage:
    """Safe identity for an embedded image/region extracted from a page."""

    document_id: int
    version_id: int
    page_number: int
    asset_type: str


def embedded_region_lineage(
    *, document_id: int | None, version_id: int | None, page_number: int | None,
    asset_type: str = "IMAGE",
) -> EmbeddedRegionLineage | None:
    """Return lineage only when all authoritative coordinates are reliable.

    Unsupported containers/renderers must degrade by returning ``None``;
    callers must not invent page numbers or document ownership.
    """
    if type(document_id) is not int or document_id < 1:
        return None
    if type(version_id) is not int or version_id < 1:
        return None
    if type(page_number) is not int or page_number < 1:
        return None
    if asset_type not in {"IMAGE", "REGION"}:
        return None
    return EmbeddedRegionLineage(document_id, version_id, page_number, asset_type)


@dataclass(frozen=True, slots=True)
class VisualValidation:
    checksum: str
    perceptual_hash: str
    content_type: str
    width: int
    height: int
    size: int


def _pillow():
    try:
        from PIL import Image, ImageFile, UnidentifiedImageError
        return Image, ImageFile, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - dependency is part of backend
        raise VisualValidationError("visual decoder is unavailable") from exc


def validate_visual_bytes(data: bytes, content_type: str) -> VisualValidation:
    """Decode only within configured byte/pixel budgets and return safe signals."""
    if not data or len(data) > settings.max_upload_bytes:
        raise VisualValidationError("visual payload exceeds byte budget")
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized not in _VISUAL_TYPES:
        raise VisualValidationError("visual content type is not allowed")
    _reject_trailing_polyglot(data, normalized)
    Image, ImageFile, UnidentifiedImageError = _pillow()
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width < 1 or height < 1 or width * height > settings.max_image_pixels:
                raise VisualValidationError("visual dimensions exceed pixel budget")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
            gray = image.convert("L").resize((8, 8))
            pixels = list(gray.getdata())
            mean = sum(pixels) / len(pixels)
            bits = "".join("1" if pixel >= mean else "0" for pixel in pixels)
            perceptual = f"{int(bits, 2):016x}"
    except VisualValidationError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise VisualValidationError("visual payload cannot be safely decoded") from exc
    return VisualValidation(hashlib.sha256(data).hexdigest(), perceptual, normalized, width, height, len(data))


def normalize_visual_derivative(data: bytes, content_type: str, *, output_format: str = "PNG") -> bytes:
    """Create an orientation-normalized, metadata-free derivative in memory."""
    validate_visual_bytes(data, content_type)
    Image, _, _ = _pillow()
    try:
        from PIL import ImageOps

        with Image.open(BytesIO(data)) as image:
            normalized = ImageOps.exif_transpose(image)
            fmt = output_format.upper()
            if fmt == "JPEG":
                normalized = normalized.convert("RGB")
            elif fmt == "PNG":
                normalized = normalized.convert("RGBA" if "A" in normalized.getbands() else "RGB")
            else:
                raise VisualValidationError("derivative format is not allowed")
            output = BytesIO()
            normalized.save(output, format=fmt, optimize=True, exif=b"")
            result = output.getvalue()
    except (OSError, ValueError) as exc:
        raise VisualValidationError("visual derivative encoding failed") from exc
    validate_visual_bytes(result, "image/jpeg" if fmt == "JPEG" else "image/png")
    return result


def _isolated_derivative_worker(
    connection: multiprocessing.connection.Connection,
    data: bytes,
    content_type: str,
    output_format: str,
    memory_limit_bytes: int,
    max_output_bytes: int,
) -> None:
    try:
        bounded_limit = _address_space_limit(memory_limit_bytes // (1024 * 1024))
        resource.setrlimit(resource.RLIMIT_AS, (bounded_limit, bounded_limit))
    except (AttributeError, OSError, ValueError):
        pass
    try:
        result = normalize_visual_derivative(data, content_type, output_format=output_format)
        if len(result) > max_output_bytes:
            raise VisualValidationError("visual derivative exceeds output budget")
        connection.send((True, result))
    except Exception as exc:
        connection.send((False, f"{type(exc).__name__}: {exc}"[:160]))
    finally:
        connection.close()


def normalize_visual_derivative_isolated(
    data: bytes,
    content_type: str,
    *,
    output_format: str = "PNG",
    timeout_seconds: float = 10.0,
    memory_limit_mb: int = 512,
    max_output_bytes: int | None = None,
) -> bytes:
    """Normalize a visual derivative in a killable, resource-limited worker."""
    if timeout_seconds <= 0 or memory_limit_mb < 16:
        raise ValueError("invalid visual worker bounds")
    output_limit = max_output_bytes or settings.max_upload_bytes
    if output_limit < 1:
        raise ValueError("invalid derivative output limit")
    parent, child = multiprocessing.Pipe(duplex=False)
    # Spawn keeps the bounded derivative worker isolated from the caller's
    # SQLAlchemy/model threads; the parent drains the pipe below so large PNG
    # derivatives cannot deadlock the child on a full OS pipe buffer.
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_isolated_derivative_worker,
        args=(child, data, content_type, output_format, memory_limit_mb * 1024 * 1024, output_limit),
    )
    process.start()
    child.close()
    # Drain the pipe while the child is running.  Joining first deadlocks when
    # a valid derivative is larger than the OS pipe buffer (the Lenovo image
    # produces a ~1.2 MB normalized PNG).
    deadline = monotonic() + timeout_seconds
    result = None
    try:
        while monotonic() < deadline:
            if parent.poll(0.05):
                result = parent.recv()
                break
            if not process.is_alive():
                break
        if result is None and process.is_alive():
            process.terminate()
            process.join(1.0)
            raise VisualValidationError("visual derivative worker exceeded time budget")
        process.join(1.0)
        if result is None:
            raise VisualValidationError("visual derivative worker failed without a result")
        ok, result = result
        if not ok:
            raise VisualValidationError(str(result))
        if not isinstance(result, bytes) or len(result) > output_limit:
            raise VisualValidationError("visual derivative exceeds output budget")
        return result
    finally:
        parent.close()


def _isolated_validate_worker(
    connection: multiprocessing.connection.Connection,
    data: bytes,
    content_type: str,
    memory_limit_bytes: int,
) -> None:
    try:
        bounded_limit = _address_space_limit(memory_limit_bytes // (1024 * 1024))
        resource.setrlimit(resource.RLIMIT_AS, (bounded_limit, bounded_limit))
    except (AttributeError, OSError, ValueError):
        pass
    try:
        signal = validate_visual_bytes(data, content_type)
        connection.send((True, (signal.checksum, signal.perceptual_hash, signal.content_type, signal.width, signal.height, signal.size)))
    except Exception as exc:  # bounded error crosses the process boundary
        connection.send((False, str(exc)[:160]))
    finally:
        connection.close()


def validate_visual_bytes_isolated(
    data: bytes,
    content_type: str,
    *,
    timeout_seconds: float = 10.0,
    memory_limit_mb: int = 512,
) -> VisualValidation:
    """Validate/decode in a killable, resource-limited worker process."""
    if timeout_seconds <= 0 or memory_limit_mb < 16:
        raise ValueError("invalid visual worker bounds")
    parent, child = multiprocessing.Pipe(duplex=False)
    # Fork avoids re-importing a mutable application module under the local
    # single-host profile; Windows falls back to spawn where fork is absent.
    context = multiprocessing.get_context("fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn")
    process = context.Process(
        target=_isolated_validate_worker,
        args=(child, data, content_type, memory_limit_mb * 1024 * 1024),
    )
    process.start()
    child.close()
    process.join(timeout_seconds)
    try:
        if process.is_alive():
            process.terminate()
            process.join(1.0)
            raise VisualValidationError("visual worker exceeded time budget")
        if not parent.poll():
            raise VisualValidationError("visual worker failed without a result")
        ok, result = parent.recv()
        if not ok:
            raise VisualValidationError(str(result))
        if not isinstance(result, tuple) or len(result) != 6:
            raise VisualValidationError("visual worker returned an invalid result")
        return VisualValidation(*result)
    finally:
        parent.close()


def content_addressed_derivative_key(checksum: str) -> str:
    """Return the immutable object key shared by identical derivative bytes."""
    return immutable_key(checksum)


def deterministic_asset_key(version_id: int, asset_type: str, checksum: str, page_number: int | None = None) -> str:
    if asset_type not in {"PAGE", "IMAGE", "REGION", "THUMBNAIL"}:
        raise ValueError("unsupported visual asset type")
    value = f"{version_id}:{asset_type}:{page_number or 0}:{checksum}"
    return hashlib.sha256(value.encode()).hexdigest()


def ensure_manifest(db: Session, version_id: int, manifest_version: str, extractor_revision: str) -> models.VisualProcessingManifest:
    manifest = db.scalar(select(models.VisualProcessingManifest).where(
        models.VisualProcessingManifest.version_id == version_id,
        models.VisualProcessingManifest.manifest_version == manifest_version,
    ))
    if manifest is None:
        manifest = models.VisualProcessingManifest(version_id=version_id, manifest_version=manifest_version, extractor_revision=extractor_revision)
        db.add(manifest)
        db.flush()
    return manifest


def transition_manifest(db: Session, manifest: models.VisualProcessingManifest, *, stage: str, state: str = "RUNNING", error_code: str | None = None, error_message: str | None = None, retry_after_seconds: int | None = None) -> None:
    if stage not in _STAGES:
        raise ValueError("invalid visual processing stage")
    if manifest.state in {"READY", "DELETED"} and state == "RUNNING":
        return
    now = datetime.now(UTC)
    manifest.stage = stage
    manifest.state = state
    manifest.error_code = error_code[:80] if error_code else None
    manifest.error_message = error_message[:500] if error_message else None
    manifest.attempt_count += 1 if state == "RUNNING" else 0
    manifest.started_at = now if state == "RUNNING" else manifest.started_at
    manifest.completed_at = now if state in {"READY", "FAILED"} else None
    manifest.next_attempt_at = now + timedelta(seconds=retry_after_seconds) if retry_after_seconds else None
    db.flush()


def claim_manifest(db: Session, manifest_id: int, *, now: datetime | None = None) -> models.VisualProcessingManifest | None:
    """Claim a retryable visual manifest under the caller's transaction."""
    current = now or datetime.now(UTC)
    manifest = db.scalar(
        select(models.VisualProcessingManifest)
        .where(
            models.VisualProcessingManifest.id == manifest_id,
            models.VisualProcessingManifest.state.in_(("PENDING", "FAILED")),
            (models.VisualProcessingManifest.next_attempt_at.is_(None) | (models.VisualProcessingManifest.next_attempt_at <= current)),
        )
        .with_for_update()
    )
    if manifest is None:
        return None
    transition_manifest(db, manifest, stage=manifest.stage, state="RUNNING")
    return manifest


def transition_asset_lifecycle(db: Session, asset: models.VisualAsset, target: str) -> None:
    """Apply monotonic lifecycle transitions; stale derivatives cannot resurrect."""
    if target not in _LIFECYCLE_TRANSITIONS:
        raise ValueError("invalid visual asset lifecycle state")
    if target == asset.lifecycle_state:
        return
    if target not in _LIFECYCLE_TRANSITIONS.get(asset.lifecycle_state, set()):
        raise ValueError("invalid visual asset lifecycle transition")
    asset.lifecycle_state = target
    asset.updated_at = datetime.now(UTC)
    db.flush()


def register_asset(db: Session, *, document_id: int, version_id: int, asset_type: str, file_key: str | None, content_type: str, payload: bytes, page_number: int | None = None, source_asset: models.VisualAsset | None = None, relationship_type: str = "DERIVED_FROM") -> models.VisualAsset:
    signal = validate_visual_bytes(payload, content_type)
    key = deterministic_asset_key(version_id, asset_type, signal.checksum, page_number)
    existing = db.scalar(select(models.VisualAsset).where(models.VisualAsset.asset_key == key))
    if existing is not None:
        return existing
    asset = models.VisualAsset(asset_key=key, document_id=document_id, version_id=version_id, asset_type=asset_type, page_number=page_number, source_asset_id=source_asset.id if source_asset else None, file_key=file_key or content_addressed_derivative_key(signal.checksum), content_type=signal.content_type, checksum=signal.checksum, perceptual_hash=signal.perceptual_hash, width=signal.width, height=signal.height, size=signal.size)
    db.add(asset)
    db.flush()
    if source_asset is not None:
        db.add(models.VisualAssetLineage(asset_id=asset.id, source_asset_id=source_asset.id, relationship_type=relationship_type))
        db.flush()
    return asset


def tombstone_version_assets(db: Session, version_id: int) -> int:
    result = db.execute(update(models.VisualAsset).where(models.VisualAsset.version_id == version_id, models.VisualAsset.lifecycle_state.in_(("ACTIVE", "SUPERSEDED", "QUARANTINED"))).values(lifecycle_state="TOMBSTONED", updated_at=datetime.now(UTC)))
    return int(result.rowcount or 0)


__all__ = ["VisualValidation", "VisualValidationError", "claim_manifest", "content_addressed_derivative_key", "deterministic_asset_key", "ensure_manifest", "normalize_visual_derivative", "normalize_visual_derivative_isolated", "register_asset", "transition_asset_lifecycle", "transition_manifest", "tombstone_version_assets", "validate_visual_bytes", "validate_visual_bytes_isolated"]
