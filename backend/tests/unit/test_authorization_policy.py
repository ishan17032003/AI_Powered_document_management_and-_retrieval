"""Truth table for the database-free authorization policy evaluator."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.domain.authorization import (
    AccessEffect,
    AccessRule,
    AuthorizationDecision,
    AuthorizationReason,
    HardPolicyGates,
)
from app.domain.resources import (
    PrincipalRef,
    PrincipalSet,
    ResourceAncestry,
    ResourceRef,
)
from app.services.authorization_service import evaluate_authorization

NOW = datetime(2026, 7, 27, 6, 0, tzinfo=UTC)
POLICY_VERSION = 42

USER = PrincipalRef.user(7)
OTHER_USER = PrincipalRef.user(8)
GROUP_ALPHA = PrincipalRef.group(11)
GROUP_BETA = PrincipalRef.group(12)
OTHER_GROUP = PrincipalRef.group(13)
PRINCIPALS = PrincipalSet(USER, [GROUP_ALPHA, GROUP_BETA])

GLOBAL = ResourceRef.global_scope()
ROOT_CABINET = ResourceRef.cabinet(100)
NEAREST_CABINET = ResourceRef.cabinet(101)
ROOT_FOLDER = ResourceRef.folder(200)
NEAREST_FOLDER = ResourceRef.folder(201)
DOCUMENT = ResourceRef.document(300)
UNRELATED_FOLDER = ResourceRef.folder(999)
ANCESTRY = ResourceAncestry(
    [
        GLOBAL,
        ROOT_CABINET,
        NEAREST_CABINET,
        ROOT_FOLDER,
        NEAREST_FOLDER,
        DOCUMENT,
    ]
)

PASSING_GATES = HardPolicyGates(
    authenticated=True,
    account_active=True,
    security_boundary_allowed=True,
    hard_policy_allowed=True,
)


def rule(
    rule_id: int,
    *,
    principal: PrincipalRef = USER,
    permission: str = "VIEW",
    resource: ResourceRef = DOCUMENT,
    effect: AccessEffect = AccessEffect.ALLOW,
    inherits: bool = False,
    active: bool = True,
    expires_at: datetime | None = None,
) -> AccessRule:
    return AccessRule(
        rule_id=rule_id,
        principal=principal,
        permission=permission,
        resource=resource,
        effect=effect,
        inherits=inherits,
        active=active,
        expires_at=expires_at,
    )


def decide(
    rules: list[AccessRule],
    *,
    effective_permissions: frozenset[str] = frozenset({"VIEW"}),
    permission: str = "VIEW",
    gates: HardPolicyGates = PASSING_GATES,
) -> AuthorizationDecision:
    return evaluate_authorization(
        gates=gates,
        effective_permissions=effective_permissions,
        principals=PRINCIPALS,
        permission=permission,
        ancestry=ANCESTRY,
        rules=rules,
        policy_version=POLICY_VERSION,
        now=NOW,
    )


@pytest.mark.parametrize(
    (
        "case",
        "rules",
        "expected_allowed",
        "expected_reason",
        "expected_rule_id",
    ),
    [
        (
            "default deny with no rules",
            [],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "unrelated user is ignored",
            [rule(1, principal=OTHER_USER)],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "unrelated group is ignored",
            [rule(2, principal=OTHER_GROUP)],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "unrelated resource is ignored",
            [rule(3, resource=UNRELATED_FOLDER, inherits=True)],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "unrelated permission is ignored",
            [rule(4, permission="DOWNLOAD")],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "inactive rule is ignored",
            [rule(5, active=False)],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "already expired rule is ignored",
            [rule(6, expires_at=NOW - timedelta(microseconds=1))],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "expiry boundary is exclusive",
            [rule(7, expires_at=NOW)],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "rule one microsecond before expiry is active",
            [rule(8, expires_at=NOW + timedelta(microseconds=1))],
            True,
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            8,
        ),
        (
            "non-inheriting ancestor does not reach document",
            [rule(9, resource=NEAREST_FOLDER, inherits=False)],
            False,
            AuthorizationReason.ACL_NO_APPLICABLE_RULE,
            None,
        ),
        (
            "inheriting ancestor reaches document",
            [rule(10, resource=NEAREST_FOLDER, inherits=True)],
            True,
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            10,
        ),
        (
            "document rule is exact without inheritance",
            [rule(11, resource=DOCUMENT, inherits=False)],
            True,
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            11,
        ),
        (
            "nearest folder beats root folder",
            [
                rule(
                    12,
                    resource=ROOT_FOLDER,
                    effect=AccessEffect.DENY,
                    inherits=True,
                ),
                rule(13, resource=NEAREST_FOLDER, inherits=True),
            ],
            True,
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            13,
        ),
        (
            "nearest same-scope cabinet beats root cabinet",
            [
                rule(14, resource=ROOT_CABINET, inherits=True),
                rule(
                    15,
                    resource=NEAREST_CABINET,
                    effect=AccessEffect.DENY,
                    inherits=True,
                ),
            ],
            False,
            AuthorizationReason.ACL_EXPLICIT_DENY,
            15,
        ),
        (
            "document allow beats inherited folder deny",
            [
                rule(
                    16,
                    resource=NEAREST_FOLDER,
                    effect=AccessEffect.DENY,
                    inherits=True,
                ),
                rule(17, resource=DOCUMENT),
            ],
            True,
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            17,
        ),
        (
            "document group deny beats broader direct-user allow",
            [
                rule(18, resource=NEAREST_FOLDER, inherits=True),
                rule(
                    19,
                    principal=GROUP_ALPHA,
                    resource=DOCUMENT,
                    effect=AccessEffect.DENY,
                ),
            ],
            False,
            AuthorizationReason.ACL_EXPLICIT_DENY,
            19,
        ),
        (
            "direct-user allow is an exception over same-scope group deny",
            [
                rule(
                    20,
                    principal=GROUP_ALPHA,
                    effect=AccessEffect.DENY,
                ),
                rule(21),
            ],
            True,
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            21,
        ),
        (
            "direct-user deny beats same-scope group allow",
            [
                rule(22, principal=GROUP_ALPHA),
                rule(23, effect=AccessEffect.DENY),
            ],
            False,
            AuthorizationReason.ACL_EXPLICIT_DENY,
            23,
        ),
        (
            "deny beats allow within direct-user tier",
            [
                rule(24),
                rule(25, effect=AccessEffect.DENY),
                rule(26),
            ],
            False,
            AuthorizationReason.ACL_EXPLICIT_DENY,
            25,
        ),
        (
            "deny beats allow across groups in group tier",
            [
                rule(27, principal=GROUP_ALPHA),
                rule(
                    28,
                    principal=GROUP_BETA,
                    effect=AccessEffect.DENY,
                ),
            ],
            False,
            AuthorizationReason.ACL_EXPLICIT_DENY,
            28,
        ),
        (
            "expired direct-user rule does not mask active group rule",
            [
                rule(29, expires_at=NOW),
                rule(
                    30,
                    principal=GROUP_ALPHA,
                    effect=AccessEffect.DENY,
                ),
            ],
            False,
            AuthorizationReason.ACL_EXPLICIT_DENY,
            30,
        ),
        (
            "stable lowest rule id represents equivalent allows",
            [rule(32), rule(31)],
            True,
            AuthorizationReason.ACL_EXPLICIT_ALLOW,
            31,
        ),
        (
            "stable lowest rule id represents equivalent denies",
            [
                rule(34, effect=AccessEffect.DENY),
                rule(33, effect=AccessEffect.DENY),
            ],
            False,
            AuthorizationReason.ACL_EXPLICIT_DENY,
            33,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_policy_truth_table(
    case: str,
    rules: list[AccessRule],
    expected_allowed: bool,
    expected_reason: AuthorizationReason,
    expected_rule_id: int | None,
) -> None:
    del case

    decision = decide(rules)

    assert decision.allowed is expected_allowed
    assert decision.reason_code is expected_reason
    assert decision.matched_rule_id == expected_rule_id
    assert decision.policy_version == POLICY_VERSION
    assert decision.ancestry is ANCESTRY


class ExplodingRules:
    """An iterable proving that earlier gates do not inspect ACL data."""

    def __iter__(self) -> Iterator[AccessRule]:
        raise AssertionError("ACL rules must not be evaluated")


@pytest.mark.parametrize(
    ("gates", "permissions", "expected_reason"),
    [
        (
            HardPolicyGates(False, True, True, True),
            frozenset({"VIEW"}),
            AuthorizationReason.SYSTEM_UNAUTHENTICATED,
        ),
        (
            HardPolicyGates(True, False, True, True),
            frozenset({"VIEW"}),
            AuthorizationReason.SYSTEM_ACCOUNT_INACTIVE,
        ),
        (
            HardPolicyGates(True, True, False, True),
            frozenset({"VIEW"}),
            AuthorizationReason.SYSTEM_SECURITY_BOUNDARY,
        ),
        (
            HardPolicyGates(True, True, True, False),
            frozenset({"VIEW"}),
            AuthorizationReason.SYSTEM_HARD_DENY,
        ),
        (
            PASSING_GATES,
            frozenset({"DOWNLOAD"}),
            AuthorizationReason.CAPABILITY_MISSING,
        ),
    ],
)
def test_hard_and_capability_denies_short_circuit_acl(
    gates: HardPolicyGates,
    permissions: frozenset[str],
    expected_reason: AuthorizationReason,
) -> None:
    decision = evaluate_authorization(
        gates=gates,
        effective_permissions=permissions,
        principals=PRINCIPALS,
        permission="VIEW",
        ancestry=ANCESTRY,
        rules=ExplodingRules(),
        policy_version=POLICY_VERSION,
        now=NOW,
    )

    assert decision == AuthorizationDecision(
        allowed=False,
        reason_code=expected_reason,
        matched_rule_id=None,
        policy_version=POLICY_VERSION,
        ancestry=ANCESTRY,
    )


def test_first_hard_gate_wins_and_cannot_be_overridden() -> None:
    decision = decide(
        [rule(40)],
        gates=HardPolicyGates(False, False, False, False),
    )

    assert decision.reason_code is AuthorizationReason.SYSTEM_UNAUTHENTICATED
    assert decision.allowed is False
    assert decision.matched_rule_id is None


def test_capability_is_permission_specific_even_when_acl_allows() -> None:
    decision = decide(
        [rule(41, permission="DOWNLOAD")],
        effective_permissions=frozenset({"VIEW"}),
        permission="DOWNLOAD",
    )

    assert decision.reason_code is AuthorizationReason.CAPABILITY_MISSING
    assert decision.allowed is False
    assert decision.matched_rule_id is None


def test_exact_non_inheriting_rule_applies_to_non_document_target() -> None:
    folder_ancestry = ResourceAncestry(
        [GLOBAL, ROOT_CABINET, NEAREST_CABINET, ROOT_FOLDER]
    )

    decision = evaluate_authorization(
        gates=PASSING_GATES,
        effective_permissions=frozenset({"VIEW"}),
        principals=PRINCIPALS,
        permission="VIEW",
        ancestry=folder_ancestry,
        rules=[rule(42, resource=ROOT_FOLDER, inherits=False)],
        policy_version=POLICY_VERSION,
        now=NOW,
    )

    assert decision.allowed is True
    assert decision.matched_rule_id == 42


def test_policy_values_are_immutable() -> None:
    access_rule = rule(50)
    gates = PASSING_GATES
    decision = decide([access_rule])

    with pytest.raises(FrozenInstanceError):
        access_rule.active = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        gates.authenticated = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"rule_id": 0}, "positive integer"),
        ({"principal": "USER:7"}, "PrincipalRef"),
        ({"permission": ""}, "non-empty"),
        ({"permission": " VIEW"}, "trimmed"),
        ({"resource": "DOC:300"}, "ResourceRef"),
        ({"effect": "ALLOW"}, "AccessEffect"),
        ({"inherits": 1}, "boolean"),
        ({"active": 1}, "boolean"),
        ({"expires_at": datetime(2026, 7, 27)}, "timezone-aware"),
    ],
)
def test_access_rule_rejects_malformed_domain_values(
    overrides: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "rule_id": 60,
        "principal": USER,
        "permission": "VIEW",
        "resource": DOCUMENT,
        "effect": AccessEffect.ALLOW,
        "inherits": False,
        "active": True,
        "expires_at": None,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        AccessRule(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        (1, True, True, True),
        (True, 1, True, True),
        (True, True, 1, True),
        (True, True, True, 1),
    ],
)
def test_hard_policy_gates_require_real_booleans(
    values: tuple[object, object, object, object],
) -> None:
    with pytest.raises(ValueError, match="boolean"):
        HardPolicyGates(*values)  # type: ignore[arg-type]


def test_evaluator_requires_injected_aware_now_when_acl_is_reached() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_authorization(
            gates=PASSING_GATES,
            effective_permissions=frozenset({"VIEW"}),
            principals=PRINCIPALS,
            permission="VIEW",
            ancestry=ANCESTRY,
            rules=[rule(70)],
            policy_version=POLICY_VERSION,
            now=datetime(2026, 7, 27),
        )


def test_policy_modules_do_not_import_database_framework_or_cache_layers() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    paths = [
        backend_dir / "app" / "domain" / "authorization.py",
        backend_dir / "app" / "services" / "authorization_service.py",
    ]
    banned_segments = {
        "cache",
        "database",
        "fastapi",
        "models",
        "repositories",
        "sqlalchemy",
    }
    violations: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if set(module.split(".")) & banned_segments:
                    violations.append(f"{path.name}:{node.lineno}:{module}")

    assert violations == []
