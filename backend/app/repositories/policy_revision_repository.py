"""Transaction-neutral authorization policy revision mutations."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import models
from ..observability import emit_event


class PolicyRevisionError(RuntimeError):
    pass


def current(db: Session) -> int:
    rows = db.execute(
        select(models.AuthorizationPolicyState.singleton_id, models.AuthorizationPolicyState.revision)
        .order_by(models.AuthorizationPolicyState.singleton_id)
        .limit(2)
    ).all()
    if len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int or rows[0][1] < 0:
        raise PolicyRevisionError("AUTHZ_POLICY_STATE_INVALID")
    return rows[0][1]


def bump(db: Session, *, actor_id: int | None) -> int:
    """Atomically bump the revision in the caller-owned transaction."""
    values: dict[str, object] = {
        "revision": models.AuthorizationPolicyState.revision + 1,
        "updated_at": datetime.now(timezone.utc),
        "updated_by": actor_id,
    }
    result = db.execute(
        update(models.AuthorizationPolicyState)
        .where(models.AuthorizationPolicyState.singleton_id == 1)
        .values(**values)
    )
    if getattr(result, "rowcount", None) != 1:
        raise PolicyRevisionError("AUTHZ_POLICY_STATE_INVALID")
    revision = current(db)
    emit_event(
        "authorization.policy_revision.bumped",
        component="authorization",
        operation="policy_revision",
        outcome="changed",
        count=revision,
    )
    return revision
