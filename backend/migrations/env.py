"""Alembic environment for the DocVault schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401
from app.model_base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_name(
    name: str | None,
    type_: str,
    _parent_names: dict[str, str | None],
) -> bool:
    """Keep FTS5's virtual/shadow tables out of relational autogeneration."""
    return not (
        type_ == "table"
        and name is not None
        and (name == "doc_fts" or name.startswith("doc_fts_"))
    )


def _database_url() -> str:
    """Return an explicitly supplied URL; never fall back to checked-in data."""
    environment_url = os.environ.get("DOCVAULT_DATABASE_URL", "").strip()
    configured_url = config.get_main_option("sqlalchemy.url").strip()
    database_url = configured_url or environment_url
    if not database_url:
        raise RuntimeError(
            "DOCVAULT_DATABASE_URL (or an explicit Alembic sqlalchemy.url) is required"
        )
    return database_url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_name=_include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using a short-lived connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_name=_include_name,
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
