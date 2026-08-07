"""Read-only relational queries used by the RAG service."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models


def accessible_documents(
    db: Session,
    allowed_ids: set[int] | None,
) -> list[models.Document]:
    query = db.query(models.Document).filter(models.Document.lifecycle_state == "ACTIVE")
    if allowed_ids is not None:
        query = query.filter(models.Document.id.in_(allowed_ids or {-1}))
    return query.all()


def get_document(db: Session, document_id: int) -> models.Document | None:
    doc = db.get(models.Document, document_id)
    if doc and doc.lifecycle_state == "ACTIVE":
        return doc
    return None
