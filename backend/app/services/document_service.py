"""Document use cases.

This module owns ingestion, listing, detail, download, and deletion workflows.
Routers provide HTTP inputs; repositories provide persistence operations.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session
from sqlalchemy import update

from .. import models, schemas
from ..config import settings
from ..domain import HardPolicyGates, ResourceRef
from ..repositories import (
    access_rule_repository,
    document_repository,
    duplicate_repository,
    job_repository,
    outbox_repository,
    search_repository,
    visible_document_repository,
)
from ..repositories.policy_revision_repository import bump as bump_policy_revision
from ..utils import file_storage, hashing
from ..utils.request_context import RequestContext
from . import (
    audit_service,
    authorization_service,
    duplicate_service,
    extraction_service,
    ingestion_pipeline,
    quarantine,
    rbac_service,
    retention_service,
    search_service,
    upload_validation,
)
from .exceptions import (
    ConflictError,
    ConfigurationError,
    GoneError,
    NotFoundError,
    PermissionDeniedError,
)

EXTRACTOR_VERSION = extraction_service.EXTRACTION_PIPELINE_VERSION
CLASSIFIER_VERSION = "rules-v1"
CHUNKER_VERSION = "document-v1"
EMBEDDING_VERSION = "disabled-v1"
INDEX_VERSION = "fts5-v1"

_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
_MAX_IMPORT_PATH_CHARS = 4096
_MAX_IMPORT_RESULT_ITEMS = 200


@dataclass(frozen=True)
class DownloadFile:
    path: Path
    content_type: str
    filename: str


@dataclass(frozen=True)
class ImportSource:
    approved_root: Path
    directory: Path
    root_device: int
    safe_id: str


@dataclass
class ImportEnumeration:
    deadline: float
    visited: int = 0
    skipped: int = 0
    errors: int = 0
    limit_reached: str | None = None
    items: list[schemas.ImportItem] = field(default_factory=list)


class _UnsafeImportPath(RuntimeError):
    """Internal path rejection whose details must never reach a response or audit."""


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _has_nested_mount(path: Path, root: Path) -> bool:
    """Return whether ``path`` crosses a mount below the approved root."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return True

    current = root
    for component in relative.parts:
        current /= component
        try:
            if os.path.ismount(current):
                return True
        except OSError:
            return True
    return False


def _resolve_import_source(requested: str) -> ImportSource:
    if not requested or len(requested) > _MAX_IMPORT_PATH_CHARS or "\x00" in requested:
        raise PermissionDeniedError("Import location is not allowed")

    candidate = Path(requested)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise PermissionDeniedError("Import location is not allowed")

    matched_root = False
    for index, configured_root in enumerate(settings.folder_import_roots, start=1):
        root = Path(configured_root)
        if not root.is_absolute() or ".." in root.parts:
            continue
        if not _within(candidate, root):
            continue
        matched_root = True
        try:
            if _has_symlink_component(root) or _has_symlink_component(candidate):
                raise _UnsafeImportPath
            resolved_root = root.resolve(strict=True)
            resolved_candidate = candidate.resolve(strict=True)
            if (
                not resolved_root.is_dir()
                or not resolved_candidate.is_dir()
                or not _within(resolved_candidate, resolved_root)
            ):
                raise _UnsafeImportPath
            root_stat = resolved_root.stat(follow_symlinks=False)
            candidate_stat = resolved_candidate.stat(follow_symlinks=False)
            if candidate_stat.st_dev != root_stat.st_dev:
                raise _UnsafeImportPath
            if _has_nested_mount(resolved_candidate, resolved_root):
                raise _UnsafeImportPath
        except (OSError, RuntimeError, _UnsafeImportPath) as exc:
            raise PermissionDeniedError("Import location is not allowed") from exc

        relative = resolved_candidate.relative_to(resolved_root).as_posix()
        relative_digest = hashing.sha256_bytes(relative.encode("utf-8"))[:12]
        return ImportSource(
            approved_root=resolved_root,
            directory=resolved_candidate,
            root_device=root_stat.st_dev,
            safe_id=f"import-root-{index}-{relative_digest}",
        )

    if matched_root:
        raise ConfigurationError("Approved import root is unavailable or unsafe")
    raise PermissionDeniedError("Import location is not allowed")


def _record_import_item(
    state: ImportEnumeration,
    *,
    filename: str,
    status: str,
    detail: str,
    document_id: int | None = None,
) -> None:
    if len(state.items) >= _MAX_IMPORT_RESULT_ITEMS:
        return
    state.items.append(
        schemas.ImportItem(
            filename=filename,
            status=status,
            document_id=document_id,
            detail=detail,
        )
    )


def _record_import_limit(state: ImportEnumeration, code: str) -> None:
    if state.limit_reached is not None:
        return
    state.limit_reached = code
    state.skipped += 1
    _record_import_item(
        state,
        filename="(limit)",
        status="skipped",
        detail=code,
    )


def _validated_import_entry(
    source: ImportSource,
    path: Path,
    *,
    require_directory: bool | None = None,
) -> os.stat_result:
    try:
        if _has_symlink_component(path):
            raise _UnsafeImportPath
        resolved = path.resolve(strict=True)
        if not _within(resolved, source.approved_root):
            raise _UnsafeImportPath
        if _has_nested_mount(resolved, source.approved_root):
            raise _UnsafeImportPath
        file_stat = path.stat(follow_symlinks=False)
        if file_stat.st_dev != source.root_device:
            raise _UnsafeImportPath
        if require_directory is True and not stat.S_ISDIR(file_stat.st_mode):
            raise _UnsafeImportPath
        if require_directory is False and not stat.S_ISREG(file_stat.st_mode):
            raise _UnsafeImportPath
        return file_stat
    except (OSError, RuntimeError, _UnsafeImportPath) as exc:
        raise _UnsafeImportPath from exc


def _iter_import_files(
    source: ImportSource,
    *,
    recursive: bool,
    state: ImportEnumeration,
) -> Iterator[tuple[Path, os.stat_result]]:
    stack: list[tuple[Path, int]] = [(source.directory, 0)]
    while stack and state.limit_reached is None:
        if time.monotonic() >= state.deadline:
            _record_import_limit(state, "wall_time_limit")
            return
        directory, depth = stack.pop()
        try:
            _validated_import_entry(source, directory, require_directory=True)
            entries = os.scandir(directory)
        except (OSError, _UnsafeImportPath):
            state.errors += 1
            _record_import_item(
                state,
                filename="(directory)",
                status="error",
                detail="directory_unavailable",
            )
            continue

        with entries:
            while True:
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                except OSError:
                    state.errors += 1
                    _record_import_item(
                        state,
                        filename="(directory)",
                        status="error",
                        detail="directory_unavailable",
                    )
                    break
                if time.monotonic() >= state.deadline:
                    _record_import_limit(state, "wall_time_limit")
                    return
                if state.visited >= settings.folder_import_max_visited_entries:
                    _record_import_limit(state, "visited_entry_limit")
                    return
                state.visited += 1
                path = Path(entry.path)
                name = entry.name

                if name in _SKIP_NAMES or name.startswith("."):
                    state.skipped += 1
                    continue
                try:
                    if entry.is_symlink():
                        state.skipped += 1
                        _record_import_item(
                            state,
                            filename=name,
                            status="skipped",
                            detail="symlink_rejected",
                        )
                        continue
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if not recursive:
                            continue
                        if depth >= settings.folder_import_max_depth:
                            state.skipped += 1
                            _record_import_item(
                                state,
                                filename=name,
                                status="skipped",
                                detail="depth_limit",
                            )
                            continue
                        _validated_import_entry(
                            source,
                            path,
                            require_directory=True,
                        )
                        stack.append((path, depth + 1))
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode):
                        state.skipped += 1
                        _record_import_item(
                            state,
                            filename=name,
                            status="skipped",
                            detail="non_regular_file",
                        )
                        continue
                    validated = _validated_import_entry(
                        source,
                        path,
                        require_directory=False,
                    )
                except (OSError, _UnsafeImportPath):
                    state.skipped += 1
                    _record_import_item(
                        state,
                        filename="(entry)",
                        status="skipped",
                        detail="unsafe_or_changed",
                    )
                    continue
                yield path, validated


def _read_import_file(
    source: ImportSource,
    path: Path,
    enumerated: os.stat_result,
) -> bytes:
    before = _validated_import_entry(source, path, require_directory=False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != source.root_device
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (opened.st_dev, opened.st_ino) != (enumerated.st_dev, enumerated.st_ino)
        ):
            raise _UnsafeImportPath

        descriptor_link = Path(f"/proc/self/fd/{descriptor}")
        if descriptor_link.exists():
            opened_path = descriptor_link.resolve(strict=True)
        else:
            opened_path = path.resolve(strict=True)
        if not _within(opened_path, source.approved_root):
            raise _UnsafeImportPath
        if _has_nested_mount(opened_path, source.approved_root):
            raise _UnsafeImportPath

        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(settings.max_upload_bytes + 1)
    except (OSError, RuntimeError, _UnsafeImportPath) as exc:
        raise _UnsafeImportPath from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _summary(document: models.Document) -> schemas.DocumentSummary:
    size = document.versions[-1].size if document.versions else None
    return schemas.DocumentSummary(
        id=document.id,
        title=document.title,
        folder_id=document.folder_id,
        doc_class=document.doc_class.name if document.doc_class else None,
        class_confidence=document.class_confidence,
        status=document.status,
        ocr_status=document.ocr_status,
        ocr_confidence=document.ocr_confidence,
        page_count=document.page_count,
        size=size,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _document_visible(
    db: Session,
    user: models.User,
    document_id: int,
    *,
    permission: str,
) -> bool:
    """Resolve one user's exact authorized document IDs.

    The visible-document repository is the SQL authorization boundary for
    direct object references.  It combines the action capability with the
    USER/GROUP ACL and complete hierarchy, and returns an empty set on a
    malformed policy.  A bounded overflow is also treated as unavailable so
    an attacker cannot turn a partial result into an allow.
    """

    try:
        visible_ids = visible_document_repository.resolve_visible_document_ids(
            db,
            user_id=user.id,
            permission=permission,
            now=datetime.now(UTC),
        )
    except visible_document_repository.VisibleDocumentResolutionUnavailable:
        return False
    return document_id in visible_ids


def _resource_authorized(
    db: Session,
    user: models.User,
    resource: ResourceRef,
    *,
    permission: str,
) -> bool:
    """Evaluate a non-document target (currently import folders) fail closed."""

    try:
        inputs = access_rule_repository.load_authorization_decision_inputs(
            db,
            user_id=user.id,
            permission=permission,
            resource=resource,
            now=datetime.now(UTC),
        )
    except access_rule_repository.AuthorizationInputUnavailable:
        return False
    decision = authorization_service.evaluate_authorization(
        gates=HardPolicyGates(
            authenticated=True,
            account_active=user.status == "active",
            security_boundary_allowed=True,
            hard_policy_allowed=True,
        ),
        effective_permissions=inputs.effective_permissions,
        principals=inputs.principals,
        permission=permission,
        ancestry=inputs.ancestry,
        rules=inputs.rules,
        policy_version=inputs.policy_version,
        now=inputs.evaluated_at,
    )
    return decision.allowed


def _require_authorized_document(
    db: Session,
    user: models.User,
    document_id: int,
    *,
    permission: str,
) -> models.Document:
    """Return a document only after its action-specific ACL has allowed it.

    Authorization runs before ORM hydration.  Missing documents and documents
    outside the caller's policy therefore share the same safe not-found
    response, preventing guessed IDs from becoming an existence oracle.
    """

    if not _document_visible(
        db,
        user,
        document_id,
        permission=permission,
    ):
        raise NotFoundError("Document not found")
    document = document_repository.get(db, document_id)
    if document is None or document.lifecycle_state != "ACTIVE":
        raise NotFoundError("Document not found")
    return document


def _folder_id(
    db: Session,
    requested_id: int | None,
    *,
    user: models.User | None = None,
) -> int:
    if requested_id is not None:
        folder = document_repository.get_folder(db, requested_id)
        if folder is None:
            raise NotFoundError("Folder not found")
        if user is not None and not _resource_authorized(
            db,
            user,
            ResourceRef.folder(requested_id),
            permission="CREATE",
        ):
            raise NotFoundError("Folder not found")
        return requested_id
    folder = document_repository.get_default_folder(db)
    if folder is None:
        raise ConfigurationError("No folder configured; run the seed script")
    if user is not None and not _resource_authorized(
        db,
        user,
        ResourceRef.folder(folder.id),
        permission="CREATE",
    ):
        raise NotFoundError("Folder not found")
    return folder.id


def ingest_document(
    db: Session,
    user: models.User,
    *,
    filename: str,
    data: bytes,
    content_type: str,
    folder_id: int | None = None,
    action: str = "UPLOAD",
    context: RequestContext | None = None,
    job: models.IngestionJob | None = None,
) -> schemas.UploadResult:
    # Both HTTP uploads and approved folder imports enter the same bounded
    # quarantine before validation, storage promotion, or extraction.
    staged = quarantine.stage_stream(
        BytesIO(data),
        directory=settings.storage_dir / ".upload-quarantine",
        max_bytes=settings.max_upload_bytes,
    )
    try:
        data = staged.path.read_bytes()
    finally:
        staged.path.unlink(missing_ok=True)
    validated = upload_validation.validate_upload(
        filename=filename, data=data, content_type=content_type
    )
    filename = validated.filename
    content_type = validated.content_type or content_type

    target_folder_id = _folder_id(db, folder_id, user=user)
    checksum = hashing.sha256_bytes(data)
    existing = duplicate_repository.find_exact_document(db, checksum)
    stored = file_storage.store_bytes(filename, data, checksum)

    try:
        document = document_repository.add_document(
            db,
            models.Document(
                folder_id=target_folder_id,
                title=filename,
                content_hash=checksum,
                status="PROCESSING",
                created_by=user.id,
            ),
        )
        version = document_repository.add_version(
            db,
            models.DocVersion(
                document_id=document.id,
                version_no=1,
                file_key=stored.key,
                filename=filename,
                content_type=content_type or "application/octet-stream",
                size=len(data),
                checksum=checksum,
                created_by=user.id,
                ocr_text="",
                extractor_version=EXTRACTOR_VERSION,
                chunker_version=CHUNKER_VERSION,
                embedding_version=EMBEDDING_VERSION,
                index_version=INDEX_VERSION,
            ),
        )
        if job is None:
            job = job_repository.create_ingestion_job(
                db,
                job_id=str(uuid4()),
                idempotency_key=f"document:{document.id}:{checksum}",
                stage_version="pipeline-v2",
                document_id=document.id,
                version_id=version.id,
            )
        else:
            job.document_id = document.id
            job.version_id = version.id
            job.stage_version = "pipeline-v2"
            job.stage = "EXTRACT"
        ingestion_pipeline.record_stage_result(
            job,
            ingestion_pipeline.IngestionStage.DEDUPLICATE,
            ingestion_pipeline.StageResultStatus.COMPLETED,
            metrics={"duplicate": existing is not None},
        )

        duplicate_of = None
        if existing is not None:
            duplicate_service.register_exact(
                db,
                primary_id=existing.id,
                duplicate_id=document.id,
            )
            duplicate_of = existing.id

        audit_service.record(
            db,
            actor=user,
            action=action,
            object_type="document",
            object_id=document.id,
            details={
                "filename": filename,
                "ocr": "queued",
                "class": None,
                "duplicate_of": duplicate_of,
            },
            context=context,
        )

        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        try:
            file_storage.delete_file(stored.key)
        except Exception:
            pass
        raise

    return schemas.UploadResult(
        id=document.id,
        job_id=job.id if job is not None else None,
        version_id=version.id,
        title=document.title,
        status=document.status,
        folder_id=document.folder_id,
        ocr_status="queued",
        doc_class=None,
        duplicate_of=duplicate_of,
        pipeline={
            "ocr": "queued",
            "index": "queued",
            "dedupCheck": "duplicate" if duplicate_of else "unique",
            "jobId": job.id if job is not None else None,
            "versions": {
                "extractor": EXTRACTOR_VERSION,
                "classifier": CLASSIFIER_VERSION,
                "chunker": CHUNKER_VERSION,
                "embedding": EMBEDDING_VERSION,
                "index": INDEX_VERSION,
            },
        },
    )


def ingest_document_idempotent(
    db: Session,
    user: models.User,
    *,
    idempotency_key: str,
    filename: str,
    data: bytes,
    content_type: str,
    folder_id: int | None = None,
    context: RequestContext | None = None,
) -> schemas.UploadResult:
    """Ingest an upload once and replay the durable result for retries."""

    job = job_repository.create_ingestion_job(
        db,
        job_id=str(uuid4()),
        idempotency_key=idempotency_key,
        stage_version="upload-v1",
    )
    if job.document_id is not None:
        document = document_repository.get(db, job.document_id)
        if document is not None:
            return schemas.UploadResult(
                id=document.id,
                job_id=getattr(job, "id", None),
                version_id=getattr(job, "version_id", None),
                title=document.title,
                status=document.status,
                folder_id=document.folder_id,
                ocr_status=document.ocr_status,
                doc_class=document.doc_class.name if document.doc_class else None,
                pipeline={"index": "replay", "dedupCheck": "replayed"},
            )

    result = ingest_document(
        db,
        user,
        filename=filename,
        data=data,
        content_type=content_type,
        folder_id=folder_id,
        context=context,
        job=job,
    )
    # Keep the idempotency row linked even when a facade/test double returns
    # the result without mutating the supplied job object.
    if job.document_id is None:
        job.document_id = result.id
    db.commit()
    return result


def ingest_document_version(
    db: Session,
    user: models.User,
    document_id: int,
    *,
    filename: str,
    data: bytes,
    content_type: str,
    context: RequestContext | None = None,
) -> schemas.UploadResult:
    """Append an authorized document version through the durable ingest path."""
    document = _require_authorized_document(db, user, document_id, permission="CREATE")
    staged = quarantine.stage_stream(
        BytesIO(data),
        directory=settings.storage_dir / ".upload-quarantine",
        max_bytes=settings.max_upload_bytes,
    )
    try:
        payload = staged.path.read_bytes()
    finally:
        staged.path.unlink(missing_ok=True)
    validated = upload_validation.validate_upload(
        filename=filename, data=payload, content_type=content_type
    )
    filename = validated.filename
    content_type = validated.content_type or content_type
    checksum = hashing.sha256_bytes(payload)
    class_name = document.doc_class.name if document.doc_class else None
    stored = file_storage.store_bytes(filename, payload, checksum, class_name=class_name)
    version_no = max((item.version_no for item in document.versions), default=0) + 1
    try:
        version = document_repository.add_version(
            db,
            models.DocVersion(
                document_id=document.id,
                version_no=version_no,
                file_key=stored.key,
                filename=filename,
                content_type=content_type or "application/octet-stream",
                size=len(payload),
                checksum=checksum,
                created_by=user.id,
                ocr_text="",
                extractor_version=EXTRACTOR_VERSION,
                chunker_version=CHUNKER_VERSION,
                embedding_version=EMBEDDING_VERSION,
                index_version=INDEX_VERSION,
            ),
        )
        job = job_repository.create_ingestion_job(
            db,
            job_id=str(uuid4()),
            idempotency_key=f"document-version:{document.id}:{version.id}:{checksum}",
            stage_version="pipeline-v2",
            document_id=document.id,
            version_id=version.id,
        )
        document.title = filename
        document.content_hash = checksum
        document.status = "PROCESSING"
        document.ocr_status = "pending"
        audit_service.record(
            db,
            actor=user,
            action="UPLOAD_VERSION",
            object_type="document",
            object_id=document.id,
            details={"version_id": version.id, "version_no": version_no, "filename": filename},
            context=context,
        )
        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        if document_repository.active_references_for_key(db, file_key=stored.key) == 0:
            file_storage.delete_file(stored.key)
        raise
    return schemas.UploadResult(
        id=document.id,
        job_id=job.id,
        version_id=version.id,
        title=document.title,
        status=document.status,
        folder_id=document.folder_id,
        ocr_status="queued",
        doc_class=None,
        duplicate_of=None,
        pipeline={"ocr": "queued", "index": "queued", "jobId": job.id, "versionNo": version_no},
    )


def import_folder(
    db: Session,
    user: models.User,
    payload: schemas.ImportRequest,
    *,
    context: RequestContext | None = None,
) -> schemas.ImportResult:
    if not settings.folder_import_enabled:
        raise PermissionDeniedError("Server folder import is disabled")
    if not settings.folder_import_roots:
        raise ConfigurationError("Server folder import has no approved roots")
    if not (
        rbac_service.has_global_permission(db, user, "ADMIN")
        or rbac_service.has_global_permission(
            db, user, rbac_service.IMPORT_SERVER_FOLDER_PERMISSION
        )
    ):
        raise PermissionDeniedError("Missing required permission: IMPORT_SERVER_FOLDER")

    source = _resolve_import_source(payload.path)

    target_folder_id = _folder_id(db, payload.folder_id, user=user)
    state = ImportEnumeration(
        deadline=time.monotonic() + settings.folder_import_max_seconds
    )
    imported = duplicates = 0
    accepted_files = aggregate_bytes = 0
    aggregate_limit = settings.folder_import_max_total_mb * 1024 * 1024

    for path, enumerated in _iter_import_files(
        source,
        recursive=payload.recursive,
        state=state,
    ):
        if accepted_files >= settings.folder_import_max_files:
            _record_import_limit(state, "accepted_file_limit")
            break
        if enumerated.st_size <= 0:
            state.skipped += 1
            _record_import_item(
                state,
                filename=path.name,
                status="skipped",
                detail="empty_file",
            )
            continue
        if enumerated.st_size > settings.max_upload_bytes:
            state.skipped += 1
            _record_import_item(
                state,
                filename=path.name,
                status="skipped",
                detail="file_size_limit",
            )
            continue
        if aggregate_bytes + enumerated.st_size > aggregate_limit:
            _record_import_limit(state, "aggregate_byte_limit")
            break

        try:
            data = _read_import_file(source, path, enumerated)
        except _UnsafeImportPath:
            state.skipped += 1
            _record_import_item(
                state,
                filename=path.name,
                status="skipped",
                detail="unsafe_or_changed",
            )
            continue
        if not data or len(data) > settings.max_upload_bytes:
            state.skipped += 1
            _record_import_item(
                state,
                filename=path.name,
                status="skipped",
                detail="file_size_limit",
            )
            continue
        if aggregate_bytes + len(data) > aggregate_limit:
            _record_import_limit(state, "aggregate_byte_limit")
            break

        accepted_files += 1
        aggregate_bytes += len(data)
        try:
            result = ingest_document(
                db,
                user,
                filename=path.name,
                data=data,
                content_type="",
                folder_id=target_folder_id,
                action="IMPORT",
                context=context,
            )
            if result.duplicate_of:
                duplicates += 1
                _record_import_item(
                    state,
                    filename=path.name,
                    status="duplicate",
                    document_id=result.id,
                    detail=f"duplicate_of_document_{result.duplicate_of}",
                )
            else:
                imported += 1
                _record_import_item(
                    state,
                    filename=path.name,
                    status="imported",
                    document_id=result.id,
                    detail=result.doc_class or "unclassified",
                )
        except Exception:
            db.rollback()
            state.errors += 1
            _record_import_item(
                state,
                filename=path.name,
                status="error",
                detail="import_failed",
            )

    audit_service.record(
        db,
        actor=user,
        action="IMPORT_FOLDER",
        object_type="folder",
        object_id=source.safe_id,
        details={
            "source_id": source.safe_id,
            "imported": imported,
            "duplicates": duplicates,
            "skipped": state.skipped,
            "errors": state.errors,
            "visited_entries": state.visited,
            "accepted_files": accepted_files,
            "accepted_bytes": aggregate_bytes,
            "limit_reached": state.limit_reached,
        },
        context=context,
    )
    db.commit()
    return schemas.ImportResult(
        path=source.safe_id,
        imported=imported,
        duplicates=duplicates,
        skipped=state.skipped,
        errors=state.errors,
        items=state.items,
    )


def list_documents(
    db: Session,
    *,
    allowed_ids: set[int] | None,
    limit: int,
) -> list[schemas.DocumentSummary]:
    documents = document_repository.list_recent(
        db,
        allowed_ids=allowed_ids,
        limit=limit,
    )
    return [_summary(document) for document in documents]


def list_documents_page(
    db: Session,
    *,
    allowed_ids: set[int] | None,
    after_id: int | None,
    limit: int,
) -> tuple[list[schemas.DocumentSummary], int | None]:
    documents = document_repository.list_after_id(
        db, allowed_ids=allowed_ids, after_id=after_id, limit=limit + 1
    )
    has_next = len(documents) > limit
    page = documents[:limit]
    return [_summary(document) for document in page], (page[-1].id if has_next else None)


def get_document_detail(
    db: Session,
    user: models.User,
    document_id: int,
    *,
    context: RequestContext | None = None,
) -> schemas.DocumentSummary:
    document = _require_authorized_document(
        db,
        user,
        document_id,
        permission="VIEW",
    )
    metadata = document_repository.list_metadata(db, document_id)
    duplicate_of = duplicate_repository.find_primary_for_duplicate(
        db,
        document_id,
    )
    summary = _summary(document)

    audit_service.record(
        db,
        actor=user,
        action="VIEW",
        object_type="document",
        object_id=document_id,
        context=context,
    )
    return schemas.DocumentDetail(
        **summary.model_dump(),
        content_hash=document.content_hash,
        language=document.language,
        ocr_text=document.versions[-1].ocr_text if document.versions else "",
        metadata=[
            schemas.MetadataOut(
                key=item.key,
                value=item.value,
                confidence=item.confidence,
            )
            for item in metadata
        ],
        versions=[
            schemas.VersionOut.model_validate(version) for version in document.versions
        ],
        is_duplicate_of=duplicate_of,
    )


def _version_to_download_file(version: models.DocVersion) -> DownloadFile:
    """Resolve a DocVersion to a DownloadFile, handling both backends.

    For the filesystem backend, the stored key is a relative path that can be
    opened directly.  For the MinIO backend we stream the object into a
    temporary file so FastAPI's FileResponse can serve it; the caller is
    responsible for unlinking the file after the response is sent (FastAPI does
    this automatically when ``background`` tasks are used, but here the temp
    file lives long enough for Starlette to read it before the GC removes it).
    """
    from ..config import settings
    from .. import storage as _storage_module

    if settings.storage_backend == "filesystem":
        path = file_storage.resolve_storage_path(version.file_key)
        if not path.exists():
            raise GoneError("File missing from storage")
        return DownloadFile(path=path, content_type=version.content_type, filename=version.filename)

    # MinIO: stream the object into a named temp file.
    import tempfile
    stream = _storage_module.object_store.open(version.file_key)
    suffix = Path(version.filename).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            tmp.write(chunk)
        tmp.flush()
        tmp.close()
    except Exception:
        import os as _os
        _os.unlink(tmp.name)
        raise
    return DownloadFile(
        path=Path(tmp.name),
        content_type=version.content_type,
        filename=version.filename,
    )


def get_download(
    db: Session,
    user: models.User,
    document_id: int,
    *,
    context: RequestContext | None = None,
) -> DownloadFile:
    document = _require_authorized_document(
        db,
        user,
        document_id,
        permission="DOWNLOAD",
    )
    if not document.versions:
        raise NotFoundError("Document not found")
    version = document.versions[-1]
    audit_service.record(
        db,
        actor=user,
        action="DOWNLOAD",
        object_type="document",
        object_id=document_id,
        context=context,
    )
    return _version_to_download_file(version)


def list_versions(
    db: Session, user: models.User, document_id: int
) -> list[schemas.VersionOut]:
    document = _require_authorized_document(db, user, document_id, permission="VIEW")
    return [schemas.VersionOut.model_validate(version) for version in document.versions]


def get_version_download(
    db: Session,
    user: models.User,
    document_id: int,
    version_id: int,
    *,
    context: RequestContext | None = None,
) -> DownloadFile:
    document = _require_authorized_document(db, user, document_id, permission="DOWNLOAD")
    version = next((item for item in document.versions if item.id == version_id), None)
    if version is None or version.storage_state == "DELETED":
        raise NotFoundError("Version not found")
    audit_service.record(
        db, actor=user, action="DOWNLOAD", object_type="document_version", object_id=version.id, context=context
    )
    return _version_to_download_file(version)


def delete_document(
    db: Session,
    user: models.User,
    document_id: int,
    *,
    context: RequestContext | None = None,
) -> None:
    document = _require_authorized_document(
        db,
        user,
        document_id,
        permission="DELETE",
    )
    retention_service.assert_deletable(document)
    file_keys = [version.file_key for version in document.versions]

    try:
        document_repository.delete_metadata(db, document_id)
        duplicate_repository.delete_members_for_documents(db, {document_id})
        group_ids = duplicate_repository.primary_group_ids(db, document_id)
        duplicate_repository.delete_members_for_groups(db, group_ids)
        duplicate_repository.delete_groups(db, group_ids)
        search_repository.remove_document(db, document_id)
        document.lifecycle_state = "TOMBSTONED"
        document.deleted_at = datetime.now(UTC)
        document.status = "ERROR"
        document.failure_code = "TOMBSTONED"
        for version in document.versions:
            version.storage_state = "DELETED"
        outbox_repository.create_outbox_event(
            db,
            event_id=str(uuid4()),
            aggregate_type="document",
            aggregate_id=str(document_id),
            event_type="document.storage.cleanup.requested",
            payload={"document_id": document_id, "file_keys": file_keys},
            idempotency_key=f"document-delete:{document_id}:{document.deleted_at.isoformat()}",
        )
        audit_service.record(
            db,
            actor=user,
            action="DELETE",
            object_type="document",
            object_id=document_id,
            details={"tombstoned": True},
            context=context,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    search_service.remove_vector(document_id)


def move_document(
    db: Session,
    user: models.User,
    document_id: int,
    folder_id: int,
    *,
    expected_updated_at: datetime,
    context: RequestContext | None = None,
) -> schemas.DocumentDetail:
    """Move a document and invalidate every authorization decision atomically."""
    document = _require_authorized_document(db, user, document_id, permission="MOVE")
    folder = document_repository.get_folder(db, folder_id)
    if folder is None or not _resource_authorized(db, user, ResourceRef.folder(folder_id), permission="MOVE"):
        raise NotFoundError("Folder not found")
    if document.updated_at != expected_updated_at:
        raise ConflictError("Document was modified; reload before moving")
    if document.folder_id != folder_id:
        changed_at = datetime.now(UTC)
        result = db.execute(
            update(models.Document)
            .where(
                models.Document.id == document_id,
                models.Document.updated_at == expected_updated_at,
            )
            .values(folder_id=folder_id, updated_at=changed_at)
        )
        if result.rowcount != 1:
            db.rollback()
            raise ConflictError("Document was modified; reload before moving")
        db.refresh(document)
        bump_policy_revision(db, actor_id=user.id)
        audit_service.record(db, actor=user, action="MOVE", object_type="document", object_id=document_id, context=context)
        db.commit()
    return _summary(document)


def update_metadata(
    db: Session,
    user: models.User,
    document_id: int,
    payload: schemas.DocumentMetadataUpdate,
    *,
    context: RequestContext | None = None,
) -> schemas.DocumentDetail:
    """Replace metadata only after VIEW/EDIT_METADATA and version validation."""
    document = _require_authorized_document(db, user, document_id, permission="EDIT_METADATA")
    if document.updated_at != payload.expected_updated_at:
        raise ConflictError("Document was modified; reload before updating metadata")
    keys = [item.key.strip() for item in payload.metadata]
    if len(keys) != len(set(keys)):
        raise ValueError("metadata keys must be unique")
    try:
        document_repository.delete_metadata(db, document_id)
        db.add_all([
            models.DocMetadata(
                document_id=document_id,
                key=item.key.strip(),
                value=item.value,
                confidence=item.confidence,
            )
            for item in payload.metadata
        ])
        changed_at = datetime.now(UTC)
        result = db.execute(
            update(models.Document)
            .where(
                models.Document.id == document_id,
                models.Document.updated_at == payload.expected_updated_at,
            )
            .values(updated_at=changed_at)
        )
        if result.rowcount != 1:
            db.rollback()
            raise ConflictError("Document was modified; reload before updating metadata")
        db.refresh(document)
        audit_service.record(db, actor=user, action="EDIT_METADATA", object_type="document", object_id=document_id, context=context)
        db.commit()
    except Exception:
        db.rollback()
        raise
    metadata = document_repository.list_metadata(db, document_id)
    summary = _summary(document)
    return schemas.DocumentDetail(
        **summary.model_dump(),
        content_hash=document.content_hash,
        language=document.language,
        ocr_text=document.versions[-1].ocr_text if document.versions else "",
        metadata=[schemas.MetadataOut(key=item.key, value=item.value, confidence=item.confidence) for item in metadata],
        versions=[schemas.VersionOut.model_validate(version) for version in document.versions],
        is_duplicate_of=duplicate_repository.find_primary_for_duplicate(db, document_id),
    )


def reclassify_document(
    db: Session,
    actor: models.User,
    document_id: int,
    new_class_id: int | None,
    *,
    context: RequestContext | None = None,
) -> schemas.DocumentSummary:
    """Change the document's class and relocate the binary to the correct MinIO bucket.

    Only Super Admin (global ADMIN permission) may call this.  For the
    filesystem backend the class is updated in the DB only; no files are moved
    because the filesystem adapter uses a content-addressed layout that is
    independent of class.
    """
    if not rbac_service.has_global_permission(db, actor, "ADMIN"):
        raise PermissionDeniedError("Only Super Admin may reclassify documents")

    document = document_repository.get(db, document_id)
    if document is None or document.lifecycle_state != "ACTIVE":
        raise NotFoundError("Document not found")

    # Resolve the new class.
    new_class: models.DocClass | None = None
    if new_class_id is not None:
        new_class = db.get(models.DocClass, new_class_id)
        if new_class is None:
            raise NotFoundError("Document class not found")

    new_class_name = new_class.name if new_class else None
    old_class_name = document.doc_class.name if document.doc_class else None

    # Move binaries in MinIO (no-op for filesystem backend).
    from ..config import settings
    from .. import storage as _storage_module

    new_file_keys: dict[int, str] = {}
    if settings.storage_backend == "minio":
        for version in document.versions:
            if version.storage_state not in ("AVAILABLE", "STAGED"):
                continue
            try:
                new_key = _storage_module.object_store.move_to_class(
                    version.file_key, new_class_name
                )
                new_file_keys[version.id] = new_key
            except Exception:  # noqa: BLE001
                # Non-fatal: DB is the authoritative record; MinIO move is
                # best-effort here.  A reconciliation job can fix orphans.
                pass

    try:
        document.class_id = new_class_id
        document.class_confidence = None  # Manual override clears auto confidence.
        for version in document.versions:
            if version.id in new_file_keys:
                version.file_key = new_file_keys[version.id]

        audit_service.record(
            db,
            actor=actor,
            action="RECLASSIFY",
            object_type="document",
            object_id=document_id,
            details={
                "old_class": old_class_name,
                "new_class": new_class_name,
            },
            context=context,
        )
        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        raise

    return _summary(document)
