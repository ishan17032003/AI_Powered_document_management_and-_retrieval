"""Backward-compatible duplicate helpers.

New application code should use ``repositories.duplicate_repository`` and
``services.duplicate_service`` directly.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .repositories import duplicate_repository
from .services import duplicate_service
from .utils.hashing import sha256_bytes, sha256_file


def find_exact_duplicate(
    db: Session,
    content_hash: str,
    exclude_document_id: int | None = None,
):
    return duplicate_repository.find_exact_document(
        db,
        content_hash,
        exclude_document_id=exclude_document_id,
    )


def register_duplicate(
    db: Session,
    primary_id: int,
    duplicate_id: int,
):
    group = duplicate_service.register_exact(
        db,
        primary_id=primary_id,
        duplicate_id=duplicate_id,
    )
    db.commit()
    return group


__all__ = [
    "find_exact_duplicate",
    "register_duplicate",
    "sha256_bytes",
    "sha256_file",
]
