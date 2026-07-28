"""Read models used by the administration dashboard."""

from __future__ import annotations

from sqlalchemy import case, distinct, func
from sqlalchemy.orm import Session

from .. import models


def dashboard_counts(
    db: Session,
    *,
    visible_document_ids: set[int] | frozenset[int],
) -> dict[str, int]:
    """Aggregate dashboard values over one already-authorized ID set.

    The dashboard is an existence-sensitive endpoint: an unfiltered count can
    disclose both document inventory and duplicate relationships.  Every
    aggregate below is constrained to the exact ACL resolver output.  Empty
    resolver output is represented by ``{-1}`` and therefore returns zero.
    """

    scoped_ids = visible_document_ids or {-1}
    document_scope = models.Document.id.in_(scoped_ids)
    document_counts = db.query(
        func.count(models.Document.id),
        func.coalesce(func.sum(case((models.Document.status == "PROCESSING", 1), else_=0)), 0),
        func.coalesce(func.sum(case((models.Document.status == "REVIEW", 1), else_=0)), 0),
    ).filter(document_scope).one()
    total_documents, processing, needs_review = document_counts
    storage_bytes = (
        db.query(func.coalesce(func.sum(models.DocVersion.size), 0))
        .join(
            models.Document,
            models.Document.id == models.DocVersion.document_id,
        )
        .filter(document_scope)
        .scalar()
        or 0
    )
    open_duplicate_groups = (
        db.query(func.count(distinct(models.DupGroup.id)))
        .filter(
            models.DupGroup.resolved.is_(False),
            models.DupGroup.primary_document_id.in_(scoped_ids),
        )
        .scalar()
        or 0
    )
    duplicate_members = (
        db.query(func.count(models.DupMember.id))
        .join(
            models.DupGroup,
            models.DupGroup.id == models.DupMember.dup_group_id,
        )
        .filter(
            models.DupMember.document_id.in_(scoped_ids),
            models.DupGroup.primary_document_id.in_(scoped_ids),
        )
        .scalar()
        or 0
    )
    return {
        "total_documents": int(total_documents),
        "processing": int(processing),
        "needs_review": int(needs_review),
        "storage_bytes": int(storage_bytes),
        "open_duplicate_groups": int(open_duplicate_groups),
        "duplicate_members": int(duplicate_members),
    }
