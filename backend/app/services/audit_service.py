"""Audit logging use cases."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .. import models
from ..repositories import audit_repository
from ..repositories.visible_document_repository import (
    VisibleDocumentResolutionUnavailable,
    resolve_visible_document_ids,
)
from ..utils.request_context import (
    RequestContext,
    context_with_actor,
    normalize_external_id,
)

_ACTION_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,59}")
_OBJECT_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,39}")
_IMPORT_SOURCE_PATTERN = re.compile(r"import-root-[1-9][0-9]*-[a-f0-9]{12}")

_NUMERIC_DETAIL_FIELDS = {
    "accepted_bytes",
    "accepted_files",
    "citations",
    "duplicate_of",
    "duplicates",
    "entry_count",
    "errors",
    "hits",
    "imported",
    "primary",
    "scoped",
    "skipped",
    "visited_entries",
    "query_length",
}
_SAFE_DETAIL_VALUES = {
    "action": {"keep_both", "keep_primary"},
    "limit_reached": {
        "accepted_file_limit",
        "aggregate_byte_limit",
        "depth_limit",
        "visited_entry_limit",
        "wall_time_limit",
    },
    "mode": {
        "claude",
        "extractive",
        "keyword",
        "ollama",
        "semantic",
        "semantic-fallback",
        "text_to_image",
        "text_to_page",
        "image_to_image",
        "hybrid",
        "vllm",
    },
    "ocr": {
        "docling",
        "error",
        "native",
        "ocr",
        "pending",
        "skipped",
        "unavailable",
    },
}

_QUERY_HASH_PATTERN = re.compile(r"hmac-sha256:[a-f0-9]{64}")

# These fields are numeric in persisted audit details but refer to other
# objects rather than being harmless counters.  A resource-scoped auditor must
# not learn one of those IDs through an otherwise visible document row.
_SCOPED_OBJECT_DETAIL_FIELDS = frozenset({"duplicate_of", "primary", "removed"})


@dataclass(frozen=True, slots=True)
class AuditEntryProjection:
    """Minimal response-shaped projection for a scoped audit row."""

    id: int
    actor_name: str
    action: str
    object_type: str
    object_id: str
    ip: str
    timestamp: datetime
    details: str


def _safe_action(value: str) -> str:
    return value if _ACTION_PATTERN.fullmatch(value) else "UNCLASSIFIED"


def _safe_object_type(value: str) -> str:
    return value if _OBJECT_TYPE_PATTERN.fullmatch(value) else "object"


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _safe_object_id(
    object_type: str,
    object_id: str | int,
    actor: models.User | None,
) -> str:
    numeric = _positive_int(object_id)
    if numeric is None and isinstance(object_id, str) and object_id.isascii():
        try:
            numeric = _positive_int(int(object_id))
        except ValueError:
            numeric = None
    if numeric is not None:
        return str(numeric)
    if object_type == "folder" and isinstance(object_id, str):
        if _IMPORT_SOURCE_PATTERN.fullmatch(object_id):
            return object_id
    if object_type == "user" and actor is not None and actor.id:
        return str(actor.id)
    # Query text, user-supplied names, filenames, and all unknown identifiers
    # are deliberately not persisted in the audit log.
    return "redacted"


def _safe_nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, 2_147_483_647)


def _safe_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def _sanitize_details(
    details: dict | None,
    context: RequestContext,
) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in (details or {}).items():
        if key in _NUMERIC_DETAIL_FIELDS:
            safe_value = _safe_nonnegative_integer(value)
            if safe_value is not None:
                sanitized[key] = safe_value
        elif key == "removed" and isinstance(value, list):
            safe_removed: list[int] = []
            for item in value[:500]:
                safe_item = _positive_int(item)
                if safe_item is not None:
                    safe_removed.append(safe_item)
            sanitized[key] = safe_removed
        elif key == "source_id" and isinstance(value, str):
            if _IMPORT_SOURCE_PATTERN.fullmatch(value):
                sanitized[key] = value
        elif key == "query_hash" and isinstance(value, str):
            if _QUERY_HASH_PATTERN.fullmatch(value):
                sanitized[key] = value
        elif key in _SAFE_DETAIL_VALUES and isinstance(value, str):
            if value in _SAFE_DETAIL_VALUES[key]:
                sanitized[key] = value

    request_id = normalize_external_id(context.request_id)
    correlation_id = normalize_external_id(context.correlation_id)
    if request_id is not None:
        sanitized["request_id"] = request_id
    if correlation_id is not None:
        sanitized["correlation_id"] = correlation_id
    return sanitized


def _scoped_details(details: object) -> str:
    """Keep only redacted, non-object detail fields in a scoped projection."""

    try:
        parsed = json.loads(details) if isinstance(details, str) else None
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return "{}"

    safe: dict[str, object] = {}
    for key, value in parsed.items():
        if key in _SCOPED_OBJECT_DETAIL_FIELDS:
            continue
        if key in _NUMERIC_DETAIL_FIELDS:
            numeric = _safe_nonnegative_integer(value)
            if numeric is not None:
                safe[key] = numeric
        elif key == "source_id" and isinstance(value, str):
            if _IMPORT_SOURCE_PATTERN.fullmatch(value):
                safe[key] = value
        elif key == "query_hash" and isinstance(value, str):
            if _QUERY_HASH_PATTERN.fullmatch(value):
                safe[key] = value
        elif key in _SAFE_DETAIL_VALUES and isinstance(value, str):
            if value in _SAFE_DETAIL_VALUES[key]:
                safe[key] = value
        elif key in {"request_id", "correlation_id"}:
            normalized = normalize_external_id(value)
            if normalized is not None:
                safe[key] = normalized
    return json.dumps(safe, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _project_scoped_entries(
    entries: list[models.AuditLog],
) -> list[AuditEntryProjection]:
    """Project rows so legacy detail blobs cannot widen the ACL boundary."""

    return [
        AuditEntryProjection(
            id=entry.id,
            actor_name=entry.actor_name,
            action=entry.action,
            object_type=entry.object_type,
            object_id=entry.object_id,
            ip=entry.ip,
            timestamp=entry.timestamp,
            details=_scoped_details(entry.details),
        )
        for entry in entries
    ]


def record(
    db: Session,
    *,
    actor: models.User | None,
    action: str,
    object_type: str = "",
    object_id: str | int = "",
    details: dict | None = None,
    context: RequestContext | None = None,
) -> None:
    """Stage an audit entry in the caller's transaction.

    Protected mutations own the transaction boundary.  Audit writes therefore
    deliberately do not commit or roll back here: the action and its audit row
    either commit together or the caller rolls both back.  Database failures
    are allowed to propagate so a protected action cannot silently succeed
    without its durable audit record.
    """
    request_context = context or RequestContext()
    event_context = context_with_actor(
        request_context,
        actor.id if actor else None,
    )
    safe_action = _safe_action(action)
    safe_object_type = _safe_object_type(object_type)
    safe_object_id = _safe_object_id(safe_object_type, object_id, actor)
    safe_details = _sanitize_details(details, request_context)
    audit_repository.add(
        db,
        models.AuditLog(
            actor_id=actor.id if actor else None,
            actor_name=event_context.actor_id or "anonymous",
            action=safe_action,
            object_type=safe_object_type,
            object_id=safe_object_id,
            ip=_safe_ip(request_context.ip),
            user_agent="",
            details=json.dumps(
                safe_details,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    # Flush validates the row now, while leaving commit/rollback to the action
    # owner.  Constraint failures therefore remain visible without creating an
    # independent audit transaction.
    db.flush()


def list_entries(
    db: Session,
    *,
    action: str | None,
    actor: str | None,
    object_id: str | None,
    limit: int,
    user: models.User | None = None,
) -> list[models.AuditLog] | list[AuditEntryProjection]:
    """List audit rows under the caller's visibility boundary.

    A caller with an explicit global ``VIEW_AUDIT`` capability retains the
    existing bounded audit filters.  A resource-scoped auditor receives only
    document rows in the exact AUTHZ-004 VIEW-ID set.  The latter path fails
    closed on any unavailable or malformed authorization input.

    ``user`` is optional for backwards-compatible internal callers; the HTTP
    route always supplies it after enforcing the capability dependency.
    """
    document_ids: frozenset[int] | None = None
    if user is not None:
        from . import rbac_service

        if not rbac_service.has_global_permission(db, user, "VIEW_AUDIT"):
            try:
                document_ids = resolve_visible_document_ids(
                    db,
                    user_id=user.id,
                    permission="VIEW",
                    now=datetime.now(UTC),
                )
            except (VisibleDocumentResolutionUnavailable, ValueError):
                document_ids = frozenset()
            except Exception:
                # A broken ACL store must never turn a scoped query into a
                # global one or disclose an object ID through an error.
                document_ids = frozenset()
    entries = audit_repository.list_entries(
        db,
        action=action,
        actor=actor,
        object_id=object_id,
        limit=limit,
        document_ids=document_ids,
    )
    if document_ids is not None:
        return _project_scoped_entries(entries)
    return entries


def list_entries_page(
    db: Session,
    *,
    action: str | None,
    actor: str | None,
    object_id: str | None,
    limit: int,
    cursor_time: datetime | None,
    cursor_id: int | None,
    user: models.User,
) -> list[models.AuditLog] | list[AuditEntryProjection]:
    document_ids: frozenset[int] | None = None
    from . import rbac_service
    if not rbac_service.has_global_permission(db, user, "VIEW_AUDIT"):
        try:
            document_ids = resolve_visible_document_ids(db, user_id=user.id, permission="VIEW", now=datetime.now(UTC))
        except Exception:
            document_ids = frozenset()
    entries = audit_repository.list_entries_after(
        db, action=action, actor=actor, object_id=object_id,
        timestamp=cursor_time, entry_id=cursor_id, limit=limit,
        document_ids=document_ids,
    )
    return _project_scoped_entries(entries) if document_ids is not None else entries
