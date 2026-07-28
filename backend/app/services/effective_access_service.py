"""Administrator-safe explanation of one effective authorization decision."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .. import models, schemas
from ..domain.authorization import HardPolicyGates
from ..domain.resources import ResourceRef, ResourceScope
from ..repositories import access_rule_repository
from .authorization_service import evaluate_authorization


def explain(
    db: Session,
    *,
    target_user_id: int,
    permission: str,
    resource: ResourceRef,
) -> schemas.EffectiveAccessOut:
    target = db.get(models.User, target_user_id)
    if target is None:
        raise LookupError("user not found")
    inputs = access_rule_repository.load_authorization_decision_inputs(
        db,
        user_id=target_user_id,
        permission=permission,
        resource=resource,
        now=datetime.now(UTC),
    )
    decision = evaluate_authorization(
        gates=HardPolicyGates(
            authenticated=True,
            account_active=target.status == "active",
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
    matched = None
    if decision.matched_rule_id is not None:
        row = db.get(models.AccessRule, decision.matched_rule_id)
        if row is not None:
            matched = schemas.EffectiveAccessRule(
                id=row.id,
                principal_type=row.principal_type,
                principal_id=row.user_id or row.group_id,
                effect=row.effect,
                inherits=row.inherits,
                reason=row.reason,
                scope_type=row.scope_type,
                scope_id=row.scope_id,
            )
    ancestry = [
        {
            "scope_type": item.scope.value,
            "scope_id": item.resource_id,
        }
        for item in inputs.ancestry.resources
    ]
    return schemas.EffectiveAccessOut(
        user_id=target_user_id,
        permission=permission,
        scope_type=resource.scope.value,
        scope_id=resource.resource_id,
        allowed=decision.allowed,
        reason_code=decision.reason_code.value,
        matched_rule=matched,
        policy_version=decision.policy_version,
        ancestry=ancestry,
    )
