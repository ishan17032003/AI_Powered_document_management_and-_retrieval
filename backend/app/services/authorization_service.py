"""Pure resource-authorization policy evaluation.

This module intentionally has no database, repository, framework, or cache
dependency. Callers resolve identity, capabilities, ancestry, and rules before
invoking the evaluator.
"""

from __future__ import annotations

from collections.abc import Iterable, Set
from datetime import datetime

from ..domain.authorization import (
    AccessEffect,
    AccessRule,
    AuthorizationDecision,
    AuthorizationReason,
    HardPolicyGates,
)
from ..domain.resources import PrincipalKind, PrincipalSet, ResourceAncestry


def _deny(
    reason_code: AuthorizationReason,
    *,
    policy_version: int,
    ancestry: ResourceAncestry,
    matched_rule_id: int | None = None,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False,
        reason_code=reason_code,
        matched_rule_id=matched_rule_id,
        policy_version=policy_version,
        ancestry=ancestry,
    )


def _hard_denial_reason(gates: HardPolicyGates) -> AuthorizationReason | None:
    if not gates.authenticated:
        return AuthorizationReason.SYSTEM_UNAUTHENTICATED
    if not gates.account_active:
        return AuthorizationReason.SYSTEM_ACCOUNT_INACTIVE
    if not gates.security_boundary_allowed:
        return AuthorizationReason.SYSTEM_SECURITY_BOUNDARY
    if not gates.hard_policy_allowed:
        return AuthorizationReason.SYSTEM_HARD_DENY
    return None


def _rule_reaches_target(
    rule: AccessRule,
    ancestry: ResourceAncestry,
) -> bool:
    if rule.resource not in ancestry:
        return False
    return rule.resource == ancestry.target or rule.inherits


def _is_active_at(rule: AccessRule, now: datetime) -> bool:
    return rule.active and (rule.expires_at is None or rule.expires_at > now)


def _stable_winner(rules: Iterable[AccessRule]) -> AccessRule:
    """Choose a stable representative when equivalent winning rules exist."""

    return min(rules, key=lambda rule: rule.rule_id)


def evaluate_authorization(
    *,
    gates: HardPolicyGates,
    effective_permissions: Set[str],
    principals: PrincipalSet,
    permission: str,
    ancestry: ResourceAncestry,
    rules: Iterable[AccessRule],
    policy_version: int,
    now: datetime,
) -> AuthorizationDecision:
    """Evaluate one action against capability and resource-ACL gates.

    Precedence is fixed and fail-closed:

    1. hard system gates;
    2. action capability;
    3. active rules for the principal, permission, and reachable ancestry;
    4. the nearest/most-specific resource on the ancestry path;
    5. direct USER rules before GROUP rules at that exact resource;
    6. DENY before ALLOW within the selected principal tier.

    Expiration uses an exclusive upper bound: a rule is expired when
    ``expires_at <= now``. ``now`` is required so tests and callers never depend
    on an ambient clock.
    """

    hard_denial = _hard_denial_reason(gates)
    if hard_denial is not None:
        return _deny(
            hard_denial,
            policy_version=policy_version,
            ancestry=ancestry,
        )

    if permission not in effective_permissions:
        return _deny(
            AuthorizationReason.CAPABILITY_MISSING,
            policy_version=policy_version,
            ancestry=ancestry,
        )

    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")

    applicable = [
        rule
        for rule in rules
        if rule.permission == permission
        and rule.principal in principals
        and _is_active_at(rule, now)
        and _rule_reaches_target(rule, ancestry)
    ]
    if not applicable:
        return _deny(
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            policy_version=policy_version,
            ancestry=ancestry,
        )

    selected_position = max(
        ancestry.specificity_of(rule.resource) for rule in applicable
    )
    selected_scope_rules = [
        rule
        for rule in applicable
        if ancestry.specificity_of(rule.resource) == selected_position
    ]

    direct_user_rules = [
        rule
        for rule in selected_scope_rules
        if rule.principal.kind is PrincipalKind.USER
    ]
    selected_tier = direct_user_rules or [
        rule
        for rule in selected_scope_rules
        if rule.principal.kind is PrincipalKind.GROUP
    ]

    explicit_denies = [
        rule for rule in selected_tier if rule.effect is AccessEffect.DENY
    ]
    if explicit_denies:
        winner = _stable_winner(explicit_denies)
        return _deny(
            AuthorizationReason.ACL_EXPLICIT_DENY,
            matched_rule_id=winner.rule_id,
            policy_version=policy_version,
            ancestry=ancestry,
        )

    explicit_allows = [
        rule for rule in selected_tier if rule.effect is AccessEffect.ALLOW
    ]
    if not explicit_allows:
        return _deny(
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            policy_version=policy_version,
            ancestry=ancestry,
        )

    winner = _stable_winner(explicit_allows)
    return AuthorizationDecision(
        allowed=True,
        reason_code=AuthorizationReason.ACL_EXPLICIT_ALLOW,
        matched_rule_id=winner.rule_id,
        policy_version=policy_version,
        ancestry=ancestry,
    )
