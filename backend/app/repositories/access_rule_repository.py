"""Bounded persistence inputs for one resource-authorization decision.

The pure evaluator deliberately has no database dependency.  This module is
the fail-closed boundary that converts persisted role, group, hierarchy, rule,
and policy-state rows into its immutable input values.  It performs no caching
and never commits or rolls back the caller-owned transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from .. import models
from ..domain.authorization import AccessEffect, AccessRule
from ..domain.resources import (
    PrincipalKind,
    PrincipalRef,
    PrincipalSet,
    ResourceAncestry,
    ResourceRef,
    ResourceScope,
)

_MAX_PERMISSION_CODE_LENGTH = 40
_HARD_MAX_EFFECTIVE_PERMISSIONS = 512
_HARD_MAX_GROUPS = 1_024
_HARD_MAX_RULES = 8_192
_HARD_MAX_ANCESTRY_RESOURCES = 256


class AuthorizationInputUnavailable(RuntimeError):
    """A safe failure that callers must map to a denied authorization decision."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Authorization inputs unavailable ({code}).")


@dataclass(frozen=True, slots=True)
class AuthorizationRepositoryLimits:
    """Fixed per-decision bounds, configurable only below hard safety ceilings."""

    max_effective_permissions: int = 128
    max_groups: int = 256
    max_rules: int = 2_048
    max_ancestry_resources: int = 64

    def __post_init__(self) -> None:
        _require_limit(
            self.max_effective_permissions,
            field="max_effective_permissions",
            hard_maximum=_HARD_MAX_EFFECTIVE_PERMISSIONS,
        )
        _require_limit(
            self.max_groups,
            field="max_groups",
            hard_maximum=_HARD_MAX_GROUPS,
        )
        _require_limit(
            self.max_rules,
            field="max_rules",
            hard_maximum=_HARD_MAX_RULES,
        )
        _require_limit(
            self.max_ancestry_resources,
            field="max_ancestry_resources",
            hard_maximum=_HARD_MAX_ANCESTRY_RESOURCES,
        )


@dataclass(frozen=True, slots=True)
class AuthorizationDecisionInputs:
    """Immutable database-derived values consumed by one evaluator invocation."""

    effective_permissions: frozenset[str]
    principals: PrincipalSet
    ancestry: ResourceAncestry
    rules: tuple[AccessRule, ...]
    policy_version: int
    evaluated_at: datetime


_ANCESTRY_QUERY = text(
    """
    WITH RECURSIVE
    target_folder(id, cabinet_id, parent_id) AS (
        SELECT f.id, f.cabinet_id, f.parent_id
        FROM folders AS f
        WHERE
            (:scope_type = 'FOLDER' AND f.id = :resource_id)
            OR
            (
                :scope_type = 'DOC'
                AND f.id = (
                    SELECT d.folder_id
                    FROM documents AS d
                    WHERE d.id = :resource_id
                )
            )
    ),
    folder_path(id, cabinet_id, parent_id, depth, visited, cycle) AS (
        SELECT
            tf.id,
            tf.cabinet_id,
            tf.parent_id,
            1,
            ',' || CAST(tf.id AS TEXT) || ',',
            0
        FROM target_folder AS tf

        UNION ALL

        SELECT
            parent.id,
            parent.cabinet_id,
            parent.parent_id,
            child.depth + 1,
            child.visited || CAST(parent.id AS TEXT) || ',',
            CASE
                WHEN instr(
                    child.visited,
                    ',' || CAST(parent.id AS TEXT) || ','
                ) > 0
                THEN 1
                ELSE 0
            END
        FROM folders AS parent
        JOIN folder_path AS child ON parent.id = child.parent_id
        WHERE child.cycle = 0 AND child.depth <= :max_depth
    ),
    target_cabinet(id, parent_id) AS (
        SELECT c.id, c.parent_id
        FROM cabinets AS c
        WHERE
            (:scope_type = 'CABINET' AND c.id = :resource_id)
            OR
            (
                :scope_type IN ('FOLDER', 'DOC')
                AND c.id = (SELECT tf.cabinet_id FROM target_folder AS tf)
            )
    ),
    cabinet_path(id, parent_id, depth, visited, cycle) AS (
        SELECT
            tc.id,
            tc.parent_id,
            1,
            ',' || CAST(tc.id AS TEXT) || ',',
            0
        FROM target_cabinet AS tc

        UNION ALL

        SELECT
            parent.id,
            parent.parent_id,
            child.depth + 1,
            child.visited || CAST(parent.id AS TEXT) || ',',
            CASE
                WHEN instr(
                    child.visited,
                    ',' || CAST(parent.id AS TEXT) || ','
                ) > 0
                THEN 1
                ELSE 0
            END
        FROM cabinets AS parent
        JOIN cabinet_path AS child ON parent.id = child.parent_id
        WHERE child.cycle = 0 AND child.depth <= :max_depth
    )
    SELECT
        'CABINET' AS resource_type,
        cp.id AS resource_id,
        cp.parent_id AS parent_id,
        NULL AS cabinet_id,
        cp.depth AS depth,
        cp.cycle AS cycle
    FROM cabinet_path AS cp

    UNION ALL

    SELECT
        'FOLDER' AS resource_type,
        fp.id AS resource_id,
        fp.parent_id AS parent_id,
        fp.cabinet_id AS cabinet_id,
        fp.depth AS depth,
        fp.cycle AS cycle
    FROM folder_path AS fp
    """
)


def _require_limit(value: object, *, field: str, hard_maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= hard_maximum:
        raise ValueError(f"{field} must be between 1 and {hard_maximum}")
    return value


DEFAULT_LIMITS = AuthorizationRepositoryLimits()


def _require_positive_id(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _require_permission_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_PERMISSION_CODE_LENGTH
    ):
        raise ValueError(
            "permission must be a trimmed string of 1 to "
            f"{_MAX_PERMISSION_CODE_LENGTH} characters"
        )
    return value


def _normalize_now(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _normalize_persisted_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise AuthorizationInputUnavailable("AUTHZ_RULE_DATA_INVALID")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_effective_permission_codes(
    db: Session,
    user_id: int,
    *,
    limits: AuthorizationRepositoryLimits = DEFAULT_LIMITS,
) -> frozenset[str]:
    """Resolve all action capabilities in one bounded aggregate query.

    Role names are never loaded or inspected.  An explicit DENY for a
    permission wins over any ALLOW, matching the existing evaluator boundary.
    Assignment scope does not turn a role name into resource authority; exact
    resource visibility is independently decided by ``access_rules``.
    """

    _require_positive_id(user_id, field="user_id")
    statement = (
        select(
            models.Permission.code.label("permission_code"),
            func.max(case((models.Assignment.effect == "ALLOW", 1), else_=0)).label(
                "has_allow"
            ),
            func.max(case((models.Assignment.effect == "DENY", 1), else_=0)).label(
                "has_deny"
            ),
            func.max(
                case(
                    (
                        models.Assignment.effect.not_in(("ALLOW", "DENY")),
                        1,
                    ),
                    else_=0,
                )
            ).label("has_invalid_effect"),
        )
        .select_from(models.Assignment)
        .join(models.Role, models.Role.id == models.Assignment.role_id)
        .join(
            models.RolePermission,
            models.RolePermission.role_id == models.Role.id,
        )
        .join(
            models.Permission,
            models.Permission.id == models.RolePermission.permission_id,
        )
        .where(models.Assignment.user_id == user_id)
        .group_by(models.Permission.code)
        .order_by(models.Permission.code)
        .limit(limits.max_effective_permissions + 1)
    )
    rows = db.execute(statement).mappings().all()
    if len(rows) > limits.max_effective_permissions:
        raise AuthorizationInputUnavailable("AUTHZ_PERMISSION_LIMIT")

    effective: set[str] = set()
    for row in rows:
        try:
            code = _require_permission_code(row["permission_code"])
            effect_flags = (
                row["has_allow"],
                row["has_deny"],
                row["has_invalid_effect"],
            )
            if any(
                type(flag) is not int or flag not in (0, 1) for flag in effect_flags
            ):
                raise ValueError("role effect aggregate must be boolean")
            has_allow, has_deny, has_invalid_effect = (
                flag == 1 for flag in effect_flags
            )
        except (TypeError, ValueError):
            raise AuthorizationInputUnavailable("AUTHZ_ROLE_DATA_INVALID") from None
        if has_invalid_effect:
            raise AuthorizationInputUnavailable("AUTHZ_ROLE_DATA_INVALID")
        if has_allow and not has_deny:
            effective.add(code)
    return frozenset(effective)


def load_principal_set(
    db: Session,
    user_id: int,
    *,
    limits: AuthorizationRepositoryLimits = DEFAULT_LIMITS,
) -> PrincipalSet:
    """Load the direct user plus unique active group IDs in one query."""

    _require_positive_id(user_id, field="user_id")
    statement = (
        select(models.GroupMembership.group_id)
        .join(
            models.Group,
            models.Group.id == models.GroupMembership.group_id,
        )
        .where(
            models.GroupMembership.user_id == user_id,
            models.Group.is_active.is_(True),
        )
        .distinct()
        .order_by(models.GroupMembership.group_id)
        .limit(limits.max_groups + 1)
    )
    rows = db.execute(statement).all()
    if len(rows) > limits.max_groups:
        raise AuthorizationInputUnavailable("AUTHZ_GROUP_LIMIT")

    try:
        group_ids = tuple(
            _require_positive_id(row[0], field="group_id") for row in rows
        )
        return PrincipalSet.from_ids(user_id, group_ids)
    except (TypeError, ValueError):
        raise AuthorizationInputUnavailable("AUTHZ_GROUP_DATA_INVALID") from None


def _validate_hierarchy_rows(
    rows: list[RowMapping],
    *,
    expected_leaf_id: int | None,
    max_depth: int,
) -> list[RowMapping]:
    if not rows:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_MISSING")

    try:
        if any(type(row["depth"]) is not int for row in rows):
            raise ValueError("ancestry depth must be an integer")
        normalized = sorted(rows, key=lambda row: row["depth"])
        depths = [row["depth"] for row in normalized]
        ids = [
            _require_positive_id(row["resource_id"], field="resource_id")
            for row in normalized
        ]
        if any(type(row["cycle"]) is not int for row in normalized):
            raise ValueError("ancestry cycle marker must be an integer")
        cycles = [row["cycle"] for row in normalized]
        parent_ids = [
            None
            if row["parent_id"] is None
            else _require_positive_id(row["parent_id"], field="parent_id")
            for row in normalized
        ]
    except (KeyError, TypeError, ValueError):
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID") from None

    if (
        depths != list(range(1, len(normalized) + 1))
        or len(normalized) > max_depth
        or len(ids) != len(set(ids))
        or any(cycle not in (0, 1) for cycle in cycles)
        or any(cycles)
    ):
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID")
    if expected_leaf_id is not None and ids[0] != expected_leaf_id:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID")
    if any(
        parent_id != following_id
        for parent_id, following_id in zip(parent_ids, ids[1:], strict=False)
    ):
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID")
    if parent_ids[-1] is not None:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID")
    return normalized


def resolve_resource_ancestry(
    db: Session,
    resource: ResourceRef,
    *,
    limits: AuthorizationRepositoryLimits = DEFAULT_LIMITS,
) -> ResourceAncestry:
    """Resolve a complete root-first path using one recursive SQL statement.

    A missing target or parent, a cycle, a cabinet boundary mismatch, or a path
    beyond the configured maximum makes the input unavailable.  The repository
    never returns a partial ancestry because that could broaden inheritance.
    """

    if not isinstance(resource, ResourceRef):
        raise ValueError("resource must be a ResourceRef")
    if resource.scope is ResourceScope.GLOBAL:
        return ResourceAncestry((ResourceRef.global_scope(),))

    resource_id = _require_positive_id(resource.resource_id, field="resource_id")
    rows = (
        db.execute(
            _ANCESTRY_QUERY,
            {
                "scope_type": resource.scope.value,
                "resource_id": resource_id,
                "max_depth": limits.max_ancestry_resources,
            },
        )
        .mappings()
        .all()
    )
    cabinet_rows = [
        row for row in rows if row["resource_type"] == ResourceScope.CABINET.value
    ]
    folder_rows = [
        row for row in rows if row["resource_type"] == ResourceScope.FOLDER.value
    ]
    if len(cabinet_rows) + len(folder_rows) > limits.max_ancestry_resources:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_LIMIT")

    expected_cabinet_id = (
        resource_id if resource.scope is ResourceScope.CABINET else None
    )
    cabinets_leaf_first = _validate_hierarchy_rows(
        cabinet_rows,
        expected_leaf_id=expected_cabinet_id,
        max_depth=limits.max_ancestry_resources,
    )

    folders_leaf_first: list[RowMapping] = []
    if resource.scope in {ResourceScope.FOLDER, ResourceScope.DOC}:
        expected_folder_id = (
            resource_id if resource.scope is ResourceScope.FOLDER else None
        )
        folders_leaf_first = _validate_hierarchy_rows(
            folder_rows,
            expected_leaf_id=expected_folder_id,
            max_depth=limits.max_ancestry_resources,
        )
        try:
            leaf_cabinet_id = _require_positive_id(
                folders_leaf_first[0]["cabinet_id"],
                field="cabinet_id",
            )
            folder_cabinet_ids = {
                _require_positive_id(row["cabinet_id"], field="cabinet_id")
                for row in folders_leaf_first
            }
            cabinet_leaf_id = _require_positive_id(
                cabinets_leaf_first[0]["resource_id"],
                field="cabinet_id",
            )
        except (KeyError, TypeError, ValueError):
            raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID") from None
        if (
            folder_cabinet_ids != {leaf_cabinet_id}
            or cabinet_leaf_id != leaf_cabinet_id
        ):
            raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID")
    elif folder_rows:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID")

    ancestry_values: list[ResourceRef] = [ResourceRef.global_scope()]
    ancestry_values.extend(
        ResourceRef.cabinet(
            _require_positive_id(row["resource_id"], field="cabinet_id")
        )
        for row in reversed(cabinets_leaf_first)
    )
    ancestry_values.extend(
        ResourceRef.folder(_require_positive_id(row["resource_id"], field="folder_id"))
        for row in reversed(folders_leaf_first)
    )
    if resource.scope is ResourceScope.DOC:
        ancestry_values.append(resource)
    if len(ancestry_values) > limits.max_ancestry_resources:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_LIMIT")

    try:
        return ResourceAncestry(ancestry_values)
    except ValueError:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_INVALID") from None


def _resource_predicate(ancestry: ResourceAncestry):
    predicates = [
        and_(
            models.AccessRule.scope_type == ResourceScope.GLOBAL.value,
            models.AccessRule.scope_id.is_(None),
        )
    ]
    for scope in (
        ResourceScope.CABINET,
        ResourceScope.FOLDER,
        ResourceScope.DOC,
    ):
        resource_ids = tuple(
            resource.resource_id for resource in ancestry if resource.scope is scope
        )
        if resource_ids:
            predicates.append(
                and_(
                    models.AccessRule.scope_type == scope.value,
                    models.AccessRule.scope_id.in_(resource_ids),
                )
            )
    return or_(*predicates)


def _principal_predicate(principals: PrincipalSet):
    predicates = [
        and_(
            models.AccessRule.principal_type == PrincipalKind.USER.value,
            models.AccessRule.user_id == principals.user.principal_id,
            models.AccessRule.group_id.is_(None),
        )
    ]
    group_ids = tuple(
        principal.principal_id
        for principal in principals.ordered
        if principal.kind is PrincipalKind.GROUP
    )
    if group_ids:
        predicates.append(
            and_(
                models.AccessRule.principal_type == PrincipalKind.GROUP.value,
                models.AccessRule.group_id.in_(group_ids),
                models.AccessRule.user_id.is_(None),
            )
        )
    return or_(*predicates)


def _row_to_access_rule(row: RowMapping) -> AccessRule:
    try:
        principal_kind = PrincipalKind(row["principal_type"])
        principal_id = (
            row["user_id"] if principal_kind is PrincipalKind.USER else row["group_id"]
        )
        scope = ResourceScope(row["scope_type"])
        resource = (
            ResourceRef.global_scope()
            if scope is ResourceScope.GLOBAL
            else ResourceRef(
                scope,
                _require_positive_id(row["scope_id"], field="scope_id"),
            )
        )
        expires_at = (
            None
            if row["expires_at"] is None
            else _normalize_persisted_datetime(row["expires_at"])
        )
        if type(row["inherits"]) is not bool:
            raise ValueError("inherits must be boolean")
        return AccessRule(
            rule_id=_require_positive_id(row["rule_id"], field="rule_id"),
            principal=PrincipalRef(
                principal_kind,
                _require_positive_id(principal_id, field="principal_id"),
            ),
            permission=_require_permission_code(row["permission_code"]),
            resource=resource,
            effect=AccessEffect(row["effect"]),
            inherits=row["inherits"],
            active=True,
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError):
        raise AuthorizationInputUnavailable("AUTHZ_RULE_DATA_INVALID") from None


def load_access_rules(
    db: Session,
    *,
    permission: str,
    principals: PrincipalSet,
    ancestry: ResourceAncestry,
    now: datetime,
    limits: AuthorizationRepositoryLimits = DEFAULT_LIMITS,
) -> tuple[AccessRule, ...]:
    """Load only active, unexpired rules on the exact bounded decision path."""

    permission = _require_permission_code(permission)
    normalized_now = _normalize_now(now)
    if not isinstance(principals, PrincipalSet):
        raise ValueError("principals must be a PrincipalSet")
    if not isinstance(ancestry, ResourceAncestry):
        raise ValueError("ancestry must be a ResourceAncestry")
    if len(principals.groups) > limits.max_groups:
        raise AuthorizationInputUnavailable("AUTHZ_GROUP_LIMIT")
    if len(ancestry) > limits.max_ancestry_resources:
        raise AuthorizationInputUnavailable("AUTHZ_ANCESTRY_LIMIT")

    # SQLite stores these DateTime values without an offset.  Persisted values
    # are defined as UTC, so compare with the normalized naive UTC equivalent.
    query_now = normalized_now.replace(tzinfo=None)
    statement = (
        select(
            models.AccessRule.id.label("rule_id"),
            models.AccessRule.principal_type,
            models.AccessRule.user_id,
            models.AccessRule.group_id,
            models.Permission.code.label("permission_code"),
            models.AccessRule.scope_type,
            models.AccessRule.scope_id,
            models.AccessRule.effect,
            models.AccessRule.inherits,
            models.AccessRule.expires_at,
        )
        .join(
            models.Permission,
            models.Permission.id == models.AccessRule.permission_id,
        )
        .where(
            models.Permission.code == permission,
            models.AccessRule.is_active.is_(True),
            or_(
                models.AccessRule.expires_at.is_(None),
                models.AccessRule.expires_at > query_now,
            ),
            _principal_predicate(principals),
            _resource_predicate(ancestry),
        )
        .order_by(models.AccessRule.id)
        .limit(limits.max_rules + 1)
    )
    rows = db.execute(statement).mappings().all()
    if len(rows) > limits.max_rules:
        raise AuthorizationInputUnavailable("AUTHZ_RULE_LIMIT")

    rules = tuple(_row_to_access_rule(row) for row in rows)
    if any(rule.permission != permission for rule in rules):
        raise AuthorizationInputUnavailable("AUTHZ_RULE_DATA_INVALID")
    return rules


def load_policy_revision(db: Session) -> int:
    """Read and validate the exact singleton authorization policy revision."""

    rows = db.execute(
        select(
            models.AuthorizationPolicyState.singleton_id,
            models.AuthorizationPolicyState.revision,
        )
        .order_by(models.AuthorizationPolicyState.singleton_id)
        .limit(2)
    ).all()
    if len(rows) != 1:
        raise AuthorizationInputUnavailable("AUTHZ_POLICY_STATE_INVALID")
    singleton_id, revision = rows[0]
    if singleton_id != 1 or type(revision) is not int or revision < 0:
        raise AuthorizationInputUnavailable("AUTHZ_POLICY_STATE_INVALID")
    return revision


def load_authorization_decision_inputs(
    db: Session,
    *,
    user_id: int,
    permission: str,
    resource: ResourceRef,
    now: datetime,
    limits: AuthorizationRepositoryLimits = DEFAULT_LIMITS,
) -> AuthorizationDecisionInputs:
    """Load one complete, cache-free authorization decision input set."""

    user_id = _require_positive_id(user_id, field="user_id")
    permission = _require_permission_code(permission)
    normalized_now = _normalize_now(now)
    if not isinstance(resource, ResourceRef):
        raise ValueError("resource must be a ResourceRef")

    effective_permissions = load_effective_permission_codes(
        db,
        user_id,
        limits=limits,
    )
    principals = load_principal_set(db, user_id, limits=limits)
    ancestry = resolve_resource_ancestry(db, resource, limits=limits)
    rules = load_access_rules(
        db,
        permission=permission,
        principals=principals,
        ancestry=ancestry,
        now=normalized_now,
        limits=limits,
    )
    policy_version = load_policy_revision(db)
    return AuthorizationDecisionInputs(
        effective_permissions=effective_permissions,
        principals=principals,
        ancestry=ancestry,
        rules=rules,
        policy_version=policy_version,
        evaluated_at=normalized_now,
    )
