"""Database access for user accounts."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models


def get_by_username(db: Session, username: str) -> models.User | None:
    return db.query(models.User).filter(models.User.username == username).first()


def list_all(db: Session) -> list[models.User]:
    return db.query(models.User).order_by(models.User.id).all()


def list_after_id(db: Session, *, after_id: int | None, limit: int) -> list[models.User]:
    query = db.query(models.User)
    if after_id is not None:
        query = query.filter(models.User.id > after_id)
    return query.order_by(models.User.id.asc()).limit(limit).all()
