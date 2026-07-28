"""Database access for exact-duplicate groups."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models


def find_exact_document(
    db: Session,
    content_hash: str,
    *,
    exclude_document_id: int | None = None,
) -> models.Document | None:
    query = db.query(models.Document).filter(
        models.Document.content_hash == content_hash
    )
    if exclude_document_id is not None:
        query = query.filter(models.Document.id != exclude_document_id)
    return query.first()


def get_group(db: Session, group_id: int) -> models.DupGroup | None:
    return db.get(models.DupGroup, group_id)


def list_groups(db: Session, *, include_resolved: bool) -> list[models.DupGroup]:
    query = db.query(models.DupGroup)
    if not include_resolved:
        query = query.filter(models.DupGroup.resolved.is_(False))
    return query.order_by(models.DupGroup.created_at.desc()).all()


def list_groups_with_visible_members(
    db: Session,
    *,
    include_resolved: bool,
    visible_document_ids: set[int] | frozenset[int],
    after_id: int | None = None,
    limit: int | None = None,
) -> list[tuple[models.DupGroup, list[models.DupMember], list[models.Document]]]:
    """Return groups and visible member documents using one bounded join.

    A group's primary must also be visible; otherwise the required response
    field would disclose an unauthorized document ID.  Hidden members are
    omitted from the result, so titles and similarity metadata cannot cross the
    authorization boundary.
    """

    if not visible_document_ids:
        return []
    query = (
        db.query(models.DupGroup, models.DupMember, models.Document)
        .join(
            models.DupMember,
            models.DupMember.dup_group_id == models.DupGroup.id,
        )
        .join(
            models.Document,
            models.Document.id == models.DupMember.document_id,
        )
        .filter(
            models.DupMember.document_id.in_(visible_document_ids),
            models.DupGroup.primary_document_id.in_(visible_document_ids),
        )
    )
    if not include_resolved:
        query = query.filter(models.DupGroup.resolved.is_(False))
    if after_id is not None:
        query = query.filter(models.DupGroup.id < after_id)
    rows = query.order_by(
        models.DupGroup.created_at.desc(),
        models.DupMember.id.asc(),
    ).all()
    grouped: dict[
        int, tuple[models.DupGroup, list[models.DupMember], list[models.Document]]
    ] = {}
    for group, member, document in rows:
        entry = grouped.setdefault(group.id, (group, [], []))
        entry[1].append(member)
        entry[2].append(document)
    result = list(grouped.values())
    return result[:limit] if limit is not None else result


def find_open_exact_group(db: Session, primary_id: int) -> models.DupGroup | None:
    return (
        db.query(models.DupGroup)
        .filter(
            models.DupGroup.primary_document_id == primary_id,
            models.DupGroup.similarity_type == "exact",
            models.DupGroup.resolved.is_(False),
        )
        .first()
    )


def create_exact_group(db: Session, primary_id: int) -> models.DupGroup:
    group = models.DupGroup(
        primary_document_id=primary_id,
        similarity_type="exact",
    )
    db.add(group)
    db.flush()
    return group


def list_members(db: Session, group_id: int) -> list[models.DupMember]:
    return (
        db.query(models.DupMember)
        .filter(models.DupMember.dup_group_id == group_id)
        .all()
    )


def member_exists(db: Session, group_id: int, document_id: int) -> bool:
    return (
        db.query(models.DupMember.id)
        .filter(
            models.DupMember.dup_group_id == group_id,
            models.DupMember.document_id == document_id,
        )
        .first()
        is not None
    )


def add_member(
    db: Session,
    *,
    group_id: int,
    document_id: int,
    similarity_score: float = 1.0,
) -> models.DupMember:
    member = models.DupMember(
        dup_group_id=group_id,
        document_id=document_id,
        similarity_score=similarity_score,
    )
    db.add(member)
    return member


def find_primary_for_duplicate(db: Session, document_id: int) -> int | None:
    row = (
        db.query(models.DupGroup.primary_document_id)
        .join(models.DupMember, models.DupMember.dup_group_id == models.DupGroup.id)
        .filter(
            models.DupMember.document_id == document_id,
            models.DupGroup.primary_document_id != document_id,
        )
        .first()
    )
    return row[0] if row else None


def delete_members_for_documents(db: Session, document_ids: set[int]) -> None:
    if not document_ids:
        return
    (
        db.query(models.DupMember)
        .filter(models.DupMember.document_id.in_(document_ids))
        .delete(synchronize_session=False)
    )


def primary_group_ids(db: Session, document_id: int) -> list[int]:
    rows = (
        db.query(models.DupGroup.id)
        .filter(models.DupGroup.primary_document_id == document_id)
        .all()
    )
    return [row[0] for row in rows]


def primary_group_ids_for_documents(
    db: Session,
    document_ids: set[int] | frozenset[int],
) -> list[int]:
    if not document_ids:
        return []
    rows = (
        db.query(models.DupGroup.id)
        .filter(models.DupGroup.primary_document_id.in_(document_ids))
        .all()
    )
    return [row[0] for row in rows]


def delete_members_for_groups(db: Session, group_ids: list[int]) -> None:
    if not group_ids:
        return
    (
        db.query(models.DupMember)
        .filter(models.DupMember.dup_group_id.in_(group_ids))
        .delete(synchronize_session=False)
    )


def delete_groups(db: Session, group_ids: list[int]) -> None:
    if not group_ids:
        return
    (
        db.query(models.DupGroup)
        .filter(models.DupGroup.id.in_(group_ids))
        .delete(synchronize_session=False)
    )
