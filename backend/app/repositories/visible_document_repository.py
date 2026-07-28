"""Bounded SQL resolution of the document IDs visible to one principal.

This repository is deliberately a read-only authorization prefilter.  It does
not hydrate documents, return titles/content, or make a per-document call.  A
single recursive SQLite statement validates each document's hierarchy and
applies the same precedence as :mod:`app.services.authorization_service`.
Callers must still perform the authoritative re-check before returning or
passing retrieved content to another component.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import models
from .access_rule_repository import (
    AuthorizationInputUnavailable,
    load_effective_permission_codes,
)

_HARD_MAX_DOCUMENT_IDS = 100_000
_HARD_MAX_HIERARCHY_DEPTH = 256
_HARD_MAX_RULES = 8_192
_HARD_MAX_GROUPS = 1_024


class VisibleDocumentResolutionUnavailable(RuntimeError):
    """Raised when a bounded exact-ID resolution cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class VisibleDocumentLimits:
    """Per-request bounds; an overflow fails closed and is never truncated."""

    max_document_ids: int = 50_000
    max_hierarchy_depth: int = 64
    max_rules: int = 2_048
    max_groups: int = 256

    def __post_init__(self) -> None:
        _require_limit(
            self.max_document_ids,
            field="max_document_ids",
            maximum=_HARD_MAX_DOCUMENT_IDS,
        )
        _require_limit(
            self.max_hierarchy_depth,
            field="max_hierarchy_depth",
            maximum=_HARD_MAX_HIERARCHY_DEPTH,
        )
        _require_limit(self.max_rules, field="max_rules", maximum=_HARD_MAX_RULES)
        _require_limit(self.max_groups, field="max_groups", maximum=_HARD_MAX_GROUPS)


def _require_limit(value: object, *, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return value


DEFAULT_LIMITS = VisibleDocumentLimits()


def _require_user_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("user_id must be a positive integer")
    return value


def _require_permission(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 40
    ):
        raise ValueError("permission must be a trimmed string of 1 to 40 characters")
    return value


def _normalize_now(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


_VISIBLE_DOCUMENT_QUERY = text(
    """
WITH RECURSIVE
group_principals(principal_type, principal_id) AS (
    SELECT 'GROUP', gm.group_id
    FROM group_memberships AS gm
    JOIN groups AS g ON g.id = gm.group_id
    WHERE gm.user_id = :user_id
      AND g.is_active = 1
    ORDER BY gm.group_id
    LIMIT :group_limit_plus_one
),
principal_set(principal_type, principal_id) AS (
    SELECT 'USER', :user_id
    UNION ALL
    SELECT principal_type, principal_id
    FROM group_principals
),
group_state AS (
    SELECT CASE WHEN COUNT(*) > :max_groups THEN 1 ELSE 0 END AS overflow
    FROM group_principals
),
rule_source AS (
    SELECT
        ar.id AS rule_id,
        ar.principal_type,
        ar.user_id,
        ar.group_id,
        ar.scope_type,
        ar.scope_id,
        ar.effect,
        ar.inherits,
        CASE
            WHEN ar.principal_type NOT IN ('USER', 'GROUP') THEN 1
            WHEN ar.principal_type = 'USER'
                 AND (ar.user_id IS NULL OR ar.user_id <= 0 OR ar.group_id IS NOT NULL)
                THEN 1
            WHEN ar.principal_type = 'GROUP'
                 AND (ar.group_id IS NULL OR ar.group_id <= 0 OR ar.user_id IS NOT NULL)
                THEN 1
            WHEN ar.scope_type NOT IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC') THEN 1
            WHEN ar.scope_type = 'GLOBAL' AND ar.scope_id IS NOT NULL THEN 1
            WHEN ar.scope_type <> 'GLOBAL'
                 AND (ar.scope_id IS NULL OR ar.scope_id <= 0)
                THEN 1
            WHEN ar.effect NOT IN ('ALLOW', 'DENY') THEN 1
            WHEN ar.inherits NOT IN (0, 1) THEN 1
            WHEN ar.is_active NOT IN (0, 1) THEN 1
            ELSE 0
        END AS invalid_data
    FROM access_rules AS ar
    JOIN permissions AS p ON p.id = ar.permission_id
    JOIN principal_set AS ps
      ON (ps.principal_type = ar.principal_type
          AND ps.principal_id = CASE
              WHEN ar.principal_type = 'USER' THEN ar.user_id
              ELSE ar.group_id
          END)
    WHERE p.code = :permission
      AND ar.is_active = 1
      AND (ar.expires_at IS NULL OR ar.expires_at > :query_now)
      AND (ar.scope_type = 'DOC' OR ar.inherits = 1)
    ORDER BY ar.id
    LIMIT :rule_limit_plus_one
),
rule_state AS (
    SELECT
        COALESCE(MAX(invalid_data), 0) AS invalid_data,
        CASE WHEN COUNT(*) > :max_rules THEN 1 ELSE 0 END AS overflow
    FROM rule_source
),
folder_chain(
    doc_id, folder_id, cabinet_id, parent_id, depth, visited, cycle
) AS (
    SELECT
        d.id,
        f.id,
        f.cabinet_id,
        f.parent_id,
        1,
        ',' || CAST(f.id AS TEXT) || ',',
        0
    FROM documents AS d
    JOIN folders AS f ON f.id = d.folder_id

    UNION ALL

    SELECT
        fc.doc_id,
        parent.id,
        parent.cabinet_id,
        parent.parent_id,
        fc.depth + 1,
        fc.visited || CAST(parent.id AS TEXT) || ',',
        CASE
            WHEN instr(fc.visited, ',' || CAST(parent.id AS TEXT) || ',') > 0
                THEN 1
            ELSE 0
        END
    FROM folder_chain AS fc
    JOIN folders AS parent ON parent.id = fc.parent_id
    WHERE fc.parent_id IS NOT NULL
      AND fc.cycle = 0
      AND fc.depth < :max_hierarchy_depth
),
folder_summary AS (
    SELECT
        doc_id,
        MAX(depth) AS chain_depth,
        MAX(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END) AS terminated,
        MAX(cycle) AS cycle,
        MIN(cabinet_id) AS min_cabinet_id,
        MAX(cabinet_id) AS max_cabinet_id
    FROM folder_chain
    GROUP BY doc_id
),
valid_folders AS (
    SELECT doc_id, chain_depth, min_cabinet_id AS cabinet_id
    FROM folder_summary
    WHERE terminated = 1
      AND cycle = 0
      AND chain_depth <= :max_hierarchy_depth
      AND min_cabinet_id = max_cabinet_id
),
cabinet_chain(doc_id, cabinet_id, parent_id, depth, visited, cycle) AS (
    SELECT
        vf.doc_id,
        c.id,
        c.parent_id,
        1,
        ',' || CAST(c.id AS TEXT) || ',',
        0
    FROM valid_folders AS vf
    JOIN cabinets AS c ON c.id = vf.cabinet_id

    UNION ALL

    SELECT
        cc.doc_id,
        parent.id,
        parent.parent_id,
        cc.depth + 1,
        cc.visited || CAST(parent.id AS TEXT) || ',',
        CASE
            WHEN instr(cc.visited, ',' || CAST(parent.id AS TEXT) || ',') > 0
                THEN 1
            ELSE 0
        END
    FROM cabinet_chain AS cc
    JOIN cabinets AS parent ON parent.id = cc.parent_id
    WHERE cc.parent_id IS NOT NULL
      AND cc.cycle = 0
      AND cc.depth < :max_hierarchy_depth
),
cabinet_summary AS (
    SELECT
        doc_id,
        MAX(depth) AS chain_depth,
        MAX(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END) AS terminated,
        MAX(cycle) AS cycle
    FROM cabinet_chain
    GROUP BY doc_id
),
valid_documents AS (
    SELECT vf.doc_id, vf.chain_depth AS folder_depth, cc.chain_depth AS cabinet_depth
    FROM valid_folders AS vf
    JOIN cabinet_summary AS cc ON cc.doc_id = vf.doc_id
    WHERE cc.terminated = 1
      AND cc.cycle = 0
      AND cc.chain_depth <= :max_hierarchy_depth
),
rule_candidates(doc_id, rule_id, principal_type, effect, specificity) AS (
    SELECT vd.doc_id, rs.rule_id, rs.principal_type, rs.effect, 3000000
    FROM valid_documents AS vd
    JOIN rule_source AS rs
      ON rs.scope_type = 'DOC' AND rs.scope_id = vd.doc_id

    UNION ALL

    SELECT fc.doc_id,
           rs.rule_id,
           rs.principal_type,
           rs.effect,
           2000000 + (vd.folder_depth - fc.depth)
    FROM valid_documents AS vd
    JOIN folder_chain AS fc ON fc.doc_id = vd.doc_id
    JOIN rule_source AS rs
      ON rs.scope_type = 'FOLDER' AND rs.scope_id = fc.folder_id
    WHERE fc.cycle = 0

    UNION ALL

    SELECT cc.doc_id,
           rs.rule_id,
           rs.principal_type,
           rs.effect,
           1000000 + (vd.cabinet_depth - cc.depth)
    FROM valid_documents AS vd
    JOIN cabinet_chain AS cc ON cc.doc_id = vd.doc_id
    JOIN rule_source AS rs
      ON rs.scope_type = 'CABINET' AND rs.scope_id = cc.cabinet_id
    WHERE cc.cycle = 0

    UNION ALL

    SELECT vd.doc_id, rs.rule_id, rs.principal_type, rs.effect, 0
    FROM valid_documents AS vd
    JOIN rule_source AS rs
      ON rs.scope_type = 'GLOBAL' AND rs.scope_id IS NULL
),
selected_scope AS (
    SELECT doc_id, MAX(specificity) AS specificity
    FROM rule_candidates
    GROUP BY doc_id
),
selected_scope_rules AS (
    SELECT rc.*
    FROM rule_candidates AS rc
    JOIN selected_scope AS ss
      ON ss.doc_id = rc.doc_id AND ss.specificity = rc.specificity
),
principal_tier AS (
    SELECT doc_id, specificity,
           MAX(CASE WHEN principal_type = 'USER' THEN 1 ELSE 0 END) AS has_user
    FROM selected_scope_rules
    GROUP BY doc_id, specificity
),
decisions AS (
    SELECT sr.doc_id,
           MAX(CASE WHEN sr.effect = 'DENY' THEN 1 ELSE 0 END) AS has_deny,
           MAX(CASE WHEN sr.effect = 'ALLOW' THEN 1 ELSE 0 END) AS has_allow
    FROM selected_scope_rules AS sr
    JOIN principal_tier AS pt
      ON pt.doc_id = sr.doc_id AND pt.specificity = sr.specificity
    WHERE pt.has_user = 0 OR sr.principal_type = 'USER'
    GROUP BY sr.doc_id
)
SELECT d.doc_id
FROM decisions AS d
CROSS JOIN rule_state AS rs
CROSS JOIN group_state AS gs
WHERE rs.invalid_data = 0
  AND rs.overflow = 0
  AND gs.overflow = 0
  AND d.has_deny = 0
  AND d.has_allow = 1
ORDER BY d.doc_id
LIMIT :document_limit_plus_one
"""
)


def resolve_visible_document_ids(
    db: Session,
    *,
    user_id: int,
    permission: str,
    now: datetime,
    limits: VisibleDocumentLimits = DEFAULT_LIMITS,
) -> frozenset[int]:
    """Return exact visible document IDs for one active user and capability.

    Empty results are the safe response for missing/inactive users, missing
    capabilities, malformed hierarchy, and malformed/overflowing ACL input.
    A result-count overflow raises instead of returning a silently truncated
    prefix, allowing callers to retry with pagination at a higher layer.
    """

    user_id = _require_user_id(user_id)
    permission = _require_permission(permission)
    normalized_now = _normalize_now(now)
    if not isinstance(limits, VisibleDocumentLimits):
        raise ValueError("limits must be VisibleDocumentLimits")

    user_status = db.execute(
        select(models.User.status).where(models.User.id == user_id)
    ).scalar_one_or_none()
    if user_status != "active":
        return frozenset()

    try:
        effective_permissions = load_effective_permission_codes(db, user_id)
    except AuthorizationInputUnavailable:
        return frozenset()
    if permission not in effective_permissions:
        return frozenset()
    if db.get_bind().dialect.name != "sqlite":
        raise VisibleDocumentResolutionUnavailable("AUTHZ_SQL_DIALECT_UNSUPPORTED")

    params: dict[str, object] = {
        "user_id": user_id,
        "permission": permission,
        # SQLite DateTime values are persisted as UTC-naive text with six
        # fractional-second digits.  Bind the same canonical representation so
        # the exclusive expiry comparison remains correct at second boundaries.
        "query_now": normalized_now.replace(tzinfo=None).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        ),
        "rule_limit_plus_one": limits.max_rules + 1,
        "max_rules": limits.max_rules,
        "group_limit_plus_one": limits.max_groups + 1,
        "max_groups": limits.max_groups,
        "max_hierarchy_depth": limits.max_hierarchy_depth,
        "document_limit_plus_one": limits.max_document_ids + 1,
    }

    rows = db.execute(_VISIBLE_DOCUMENT_QUERY, params).all()
    if len(rows) > limits.max_document_ids:
        raise VisibleDocumentResolutionUnavailable("AUTHZ_DOCUMENT_ID_LIMIT")
    try:
        document_ids = tuple(row[0] for row in rows)
        if any(type(document_id) is not int or document_id <= 0 for document_id in document_ids):
            raise ValueError("document ID must be positive")
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("document IDs must be unique")
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(document_ids)


def visible_document_ids(
    db: Session,
    user_id: int,
    permission: str = "VIEW",
    *,
    now: datetime,
    limits: VisibleDocumentLimits = DEFAULT_LIMITS,
) -> frozenset[int]:
    """Compatibility-shaped alias for callers that prefer a shorter name."""

    return resolve_visible_document_ids(
        db,
        user_id=user_id,
        permission=permission,
        now=now,
        limits=limits,
    )
