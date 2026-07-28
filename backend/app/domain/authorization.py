"""Immutable values used by the pure resource-authorization evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .resources import PrincipalRef, ResourceAncestry, ResourceRef


class AccessEffect(StrEnum):
    """The only effects an access rule may carry."""

    ALLOW = "ALLOW"
    DENY = "DENY"


class AuthorizationReason(StrEnum):
    """Safe, stable reason codes suitable for audit records and API mapping."""

    SYSTEM_UNAUTHENTICATED = "SYSTEM_UNAUTHENTICATED"
    SYSTEM_ACCOUNT_INACTIVE = "SYSTEM_ACCOUNT_INACTIVE"
    SYSTEM_SECURITY_BOUNDARY = "SYSTEM_SECURITY_BOUNDARY"
    SYSTEM_HARD_DENY = "SYSTEM_HARD_DENY"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    ACL_NO_APPLICABLE_RULE = "ACL_NO_APPLICABLE_RULE"
    ACL_EXPLICIT_DENY = "ACL_EXPLICIT_DENY"
    ACL_EXPLICIT_ALLOW = "ACL_EXPLICIT_ALLOW"


def _require_bool(value: object, *, field: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")


def _require_positive_id(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _require_policy_version(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("policy_version must be a non-negative integer")
    return value


def _require_permission_code(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("permission must be a non-empty, trimmed string")
    return value


def _require_aware_datetime(value: datetime, *, field: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class AccessRule:
    """A resolved principal-to-resource access rule.

    Persistence adapters are responsible for converting database rows into this
    immutable value. The evaluator does not load or mutate rules.
    """

    rule_id: int
    principal: PrincipalRef
    permission: str
    resource: ResourceRef
    effect: AccessEffect
    inherits: bool
    active: bool = True
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_positive_id(self.rule_id, field="rule_id")
        if not isinstance(self.principal, PrincipalRef):
            raise ValueError("principal must be a PrincipalRef")
        _require_permission_code(self.permission)
        if not isinstance(self.resource, ResourceRef):
            raise ValueError("resource must be a ResourceRef")
        if not isinstance(self.effect, AccessEffect):
            raise ValueError("effect must be an AccessEffect")
        _require_bool(self.inherits, field="inherits")
        _require_bool(self.active, field="active")
        if self.expires_at is not None:
            _require_aware_datetime(self.expires_at, field="expires_at")


@dataclass(frozen=True, slots=True)
class HardPolicyGates:
    """Pre-resolved system gates that ACL rules can never override."""

    authenticated: bool
    account_active: bool
    security_boundary_allowed: bool
    hard_policy_allowed: bool

    def __post_init__(self) -> None:
        _require_bool(self.authenticated, field="authenticated")
        _require_bool(self.account_active, field="account_active")
        _require_bool(
            self.security_boundary_allowed,
            field="security_boundary_allowed",
        )
        _require_bool(self.hard_policy_allowed, field="hard_policy_allowed")


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """A safe authorization result with enough context for audit/debugging."""

    allowed: bool
    reason_code: AuthorizationReason
    matched_rule_id: int | None
    policy_version: int
    ancestry: ResourceAncestry

    def __post_init__(self) -> None:
        _require_bool(self.allowed, field="allowed")
        if not isinstance(self.reason_code, AuthorizationReason):
            raise ValueError("reason_code must be an AuthorizationReason")
        _require_policy_version(self.policy_version)
        if not isinstance(self.ancestry, ResourceAncestry):
            raise ValueError("ancestry must be a ResourceAncestry")

        if self.reason_code is AuthorizationReason.ACL_EXPLICIT_ALLOW:
            if not self.allowed:
                raise ValueError("ACL_EXPLICIT_ALLOW decisions must allow access")
        elif self.allowed:
            raise ValueError("only ACL_EXPLICIT_ALLOW may allow access")

        rule_reason = self.reason_code in {
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            AuthorizationReason.ACL_EXPLICIT_DENY,
        }
        if rule_reason:
            _require_positive_id(self.matched_rule_id, field="matched_rule_id")
        elif self.matched_rule_id is not None:
            raise ValueError("non-rule decisions cannot contain a matched_rule_id")
