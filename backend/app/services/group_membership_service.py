"""Use cases for the USER-principal group-membership lifecycle.

The service intentionally leaves commit/rollback to its caller.  HTTP wiring
and audit/outbox work are separate authorization tasks; this slice guarantees
only the membership row and policy revision share one transaction.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..repositories.group_membership_repository import (
    GroupMembershipError,
    MembershipMutation,
    add_membership,
    list_member_ids,
    remove_membership,
)


class GroupMembershipService:
    """Transaction-neutral facade for membership changes."""

    @staticmethod
    def add_user_to_group(
        db: Session,
        *,
        group_id: int,
        user_id: int,
        actor_id: int,
    ) -> MembershipMutation:
        return add_membership(
            db,
            group_id=group_id,
            user_id=user_id,
            created_by=actor_id,
        )

    @staticmethod
    def remove_user_from_group(
        db: Session,
        *,
        group_id: int,
        user_id: int,
        actor_id: int,
    ) -> MembershipMutation:
        return remove_membership(
            db,
            group_id=group_id,
            user_id=user_id,
            removed_by=actor_id,
        )

    @staticmethod
    def list_member_ids(
        db: Session,
        *,
        group_id: int,
        limit: int = 256,
    ) -> tuple[int, ...]:
        return list_member_ids(db, group_id=group_id, limit=limit)


__all__ = [
    "GroupMembershipError",
    "GroupMembershipService",
    "MembershipMutation",
]
