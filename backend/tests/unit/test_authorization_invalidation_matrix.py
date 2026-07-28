"""AUTHZ-016 regression matrix for decision isolation and rule semantics."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain.authorization import AccessEffect, AccessRule, HardPolicyGates
from app.domain.resources import (
    PrincipalRef,
    PrincipalSet,
    ResourceAncestry,
    ResourceRef,
)
from app.services.authorization_service import evaluate_authorization


def _decision(user_id: int, rule_user_id: int, *, expires_at=None, inherits=True):
    target = ResourceRef.document(20)
    return evaluate_authorization(
        gates=HardPolicyGates(True, True, True, True),
        effective_permissions={"VIEW"},
        principals=PrincipalSet.from_ids(user_id),
        permission="VIEW",
        ancestry=ResourceAncestry((ResourceRef.global_scope(), ResourceRef.cabinet(1), ResourceRef.folder(2), target)),
        rules=(
            AccessRule(
                rule_id=1,
                principal=PrincipalRef.user(rule_user_id),
                permission="VIEW",
                resource=ResourceRef.folder(2),
                effect=AccessEffect.ALLOW,
                inherits=inherits,
                expires_at=expires_at,
            ),
        ),
        policy_version=3,
        now=datetime.now(UTC),
    )


def test_same_query_different_users_never_reuses_an_allow():
    assert _decision(10, 10).allowed
    assert not _decision(11, 10).allowed


def test_inheritance_and_expiry_are_fail_closed():
    assert not _decision(10, 10, inherits=False).allowed
    assert not _decision(10, 10, expires_at=datetime.now(UTC) - timedelta(seconds=1)).allowed


def test_authz010_stored_matrix_is_bounded_and_explicit():
    artifact = (
        Path(__file__).resolve().parents[2]
        / ".."
        / "docs"
        / "evidence"
        / "WP03-N-authorization-matrix.json"
    ).resolve()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert payload["matrix_version"] == "AUTHZ-010-v1"
    assert len(cases) == 8
    assert all(
        set(case) == {"principal", "role_capability", "rule", "scope", "expected"}
        for case in cases
    )
    assert {case["expected"] for case in cases} == {"ALLOW", "DENY"}
    assert len({tuple(sorted(case.items())) for case in cases}) == len(cases)
