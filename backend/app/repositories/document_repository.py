"""Database access for documents and their hierarchy."""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload, selectinload

from .. import models


def get(db: Session, document_id: int) -> models.Document | None:
    return db.get(models.Document, document_id)


def list_recent(
    db: Session,
    *,
    allowed_ids: set[int] | None,
    limit: int,
) -> list[models.Document]:
    query = db.query(models.Document).options(
        joinedload(models.Document.doc_class), selectinload(models.Document.versions)
    ).filter(models.Document.lifecycle_state == "ACTIVE").order_by(models.Document.created_at.desc())
    if allowed_ids is not None:
        query = query.filter(models.Document.id.in_(allowed_ids or {-1}))
    return query.limit(limit).all()


def list_after_id(
    db: Session,
    *,
    allowed_ids: set[int] | None,
    after_id: int | None,
    limit: int,
) -> list[models.Document]:
    """Return an authorized, deterministic descending-ID page."""
    query = db.query(models.Document).options(
        joinedload(models.Document.doc_class), selectinload(models.Document.versions)
    ).filter(models.Document.lifecycle_state == "ACTIVE")
    if allowed_ids is not None:
        query = query.filter(models.Document.id.in_(allowed_ids or {-1}))
    if after_id is not None:
        query = query.filter(models.Document.id < after_id)
    return query.order_by(models.Document.id.desc()).limit(limit).all()


def list_accessible(
    db: Session,
    *,
    allowed_ids: set[int] | None,
) -> list[models.Document]:
    query = db.query(models.Document).options(
        joinedload(models.Document.doc_class), selectinload(models.Document.versions)
    ).filter(models.Document.lifecycle_state == "ACTIVE")
    if allowed_ids is not None:
        query = query.filter(models.Document.id.in_(allowed_ids or {-1}))
    return query.all()


def list_by_ids(
    db: Session, document_ids: set[int] | frozenset[int]
) -> list[models.Document]:
    """Load a bounded document set in one query.

    Callers pass an already-authorized ID set.  Keeping this primitive separate
    from ``get`` prevents authorization-aware aggregate paths from drifting
    into one ORM query per document.
    """

    if not document_ids:
        return []
    return db.query(models.Document).options(
        joinedload(models.Document.doc_class), selectinload(models.Document.versions)
    ).filter(
        models.Document.id.in_(document_ids),
        models.Document.lifecycle_state == "ACTIVE",
    ).all()


def get_default_folder(db: Session) -> models.Folder | None:
    return db.query(models.Folder).order_by(models.Folder.id).first()


def get_folder(db: Session, folder_id: int) -> models.Folder | None:
    return db.get(models.Folder, folder_id)


def get_or_create_class(db: Session, name: str) -> models.DocClass:
    doc_class = db.query(models.DocClass).filter(models.DocClass.name == name).first()
    if doc_class is None:
        doc_class = models.DocClass(name=name)
        db.add(doc_class)
        db.flush()
    return doc_class


def add_document(db: Session, document: models.Document) -> models.Document:
    db.add(document)
    db.flush()
    return document


def add_version(db: Session, version: models.DocVersion) -> models.DocVersion:
    db.add(version)
    db.flush()
    return version


def active_references_for_key(
    db: Session, *, file_key: str, excluding_document_id: int | None = None
) -> int:
    query = (
        db.query(models.DocVersion)
        .join(models.Document, models.Document.id == models.DocVersion.document_id)
        .filter(
            models.DocVersion.file_key == file_key,
            models.Document.lifecycle_state == "ACTIVE",
            models.DocVersion.storage_state == "AVAILABLE",
        )
    )
    if excluding_document_id is not None:
        query = query.filter(models.DocVersion.document_id != excluding_document_id)
    return query.count()


def list_metadata(db: Session, document_id: int) -> list[models.DocMetadata]:
    return (
        db.query(models.DocMetadata)
        .filter(models.DocMetadata.document_id == document_id)
        .all()
    )


def delete_metadata(db: Session, document_id: int) -> None:
    (
        db.query(models.DocMetadata)
        .filter(models.DocMetadata.document_id == document_id)
        .delete(synchronize_session=False)
    )


def delete(db: Session, document: models.Document) -> None:
    db.delete(document)


def list_tombstoned(
    db: Session,
    *,
    limit: int = 100,
) -> list[models.Document]:
    return (
        db.query(models.Document)
        .options(
            joinedload(models.Document.doc_class),
            selectinload(models.Document.versions),
        )
        .filter(models.Document.lifecycle_state == "TOMBSTONED")
        .order_by(models.Document.deleted_at.desc(), models.Document.id.desc())
        .limit(limit)
        .all()
    )
