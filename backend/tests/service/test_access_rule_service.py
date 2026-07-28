from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import models
from app.domain.resources import PrincipalKind, ResourceScope
from app.services import access_rule_service


def test_upsert_rule_validates_delegation_and_bumps_policy_revision(
    db_session, user_factory, monkeypatch
) -> None:
    actor = user_factory(username="acl-manager")
    target = user_factory(username="acl-target")
    permission = db_session.query(models.Permission).filter_by(code="VIEW").one_or_none()
    if permission is None:
        permission = models.Permission(code="VIEW")
        db_session.add(permission)
    state = db_session.get(models.AuthorizationPolicyState, 1)
    if state is None:
        state = models.AuthorizationPolicyState(singleton_id=1, revision=0)
        db_session.add(state)
    state.revision = 4
    state.updated_by = actor.id
    db_session.flush()
    monkeypatch.setattr(access_rule_service.rbac_service, "has_global_permission", lambda *_a: True)

    rule = access_rule_service.upsert_rule(
        db_session,
        actor=actor,
        principal_type=PrincipalKind.USER,
        principal_id=target.id,
        permission_code="VIEW",
        scope_type=ResourceScope.GLOBAL,
        scope_id=None,
        effect="ALLOW",
        inherits=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        reason="temporary access",
    )
    assert rule.id > 0
    assert rule.reason == "temporary access"
    assert state.revision == 5


def test_upsert_rule_rejects_expired_and_oversized_reason(
    db_session, user_factory, monkeypatch
) -> None:
    actor = user_factory(username="acl-manager-2")
    target = user_factory(username="acl-target-2")
    if db_session.query(models.Permission).filter_by(code="VIEW").one_or_none() is None:
        db_session.add(models.Permission(code="VIEW"))
    state = db_session.get(models.AuthorizationPolicyState, 1)
    if state is None:
        state = models.AuthorizationPolicyState(singleton_id=1, revision=0)
        db_session.add(state)
    state.revision = 0
    db_session.flush()
    monkeypatch.setattr(access_rule_service.rbac_service, "has_global_permission", lambda *_a: True)
    args = dict(
        actor=actor,
        principal_type=PrincipalKind.USER,
        principal_id=target.id,
        permission_code="VIEW",
        scope_type=ResourceScope.GLOBAL,
        scope_id=None,
        effect="DENY",
        inherits=False,
    )
    with pytest.raises(ValueError, match="expires_at"):
        access_rule_service.upsert_rule(db_session, **args, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(ValueError, match="reason"):
        access_rule_service.upsert_rule(db_session, **args, reason="x" * 1001)
