"""Declarative metadata shared by the application and Alembic."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all persisted DocVault entities."""
