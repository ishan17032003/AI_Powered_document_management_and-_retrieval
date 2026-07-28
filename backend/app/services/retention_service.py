"""Retention and legal-hold deletion gates."""

from __future__ import annotations

from datetime import UTC, datetime

from .. import models
from .exceptions import PermissionDeniedError


def assert_deletable(document: models.Document, *, now: datetime | None = None) -> None:
    if document.legal_hold:
        raise PermissionDeniedError("Document is under legal hold")
    expiry = document.retention_until
    if expiry is not None:
        current = now or datetime.now(UTC)
        if expiry.tzinfo is None:
            current = current.replace(tzinfo=None)
        if expiry > current:
            raise PermissionDeniedError("Document retention period has not expired")


__all__ = ["assert_deletable"]
