"""Add durable ingestion stage and pipeline metadata.

Revision ID: 20260727_0007
Revises: 20260727_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0007"
down_revision: str | None = "20260727_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if context.get_context().dialect.name != "sqlite":
        raise RuntimeError("revision 20260727_0007 is approved for SQLite only")


def upgrade() -> None:
    _require_sqlite()
    with op.batch_alter_table("doc_versions") as batch:
        batch.add_column(
            sa.Column(
                "embedding_version",
                sa.String(length=80),
                nullable=False,
                server_default=sa.text("'disabled-v1'"),
            )
        )
        batch.add_column(
            sa.Column(
                "index_version",
                sa.String(length=40),
                nullable=False,
                server_default=sa.text("'fts5-v1'"),
            )
        )
    op.execute(sa.text("UPDATE doc_versions SET embedding_version = 'disabled-v1' WHERE embedding_version IS NULL"))
    op.execute(sa.text("UPDATE doc_versions SET index_version = 'fts5-v1' WHERE index_version IS NULL"))
    with op.batch_alter_table("ingestion_jobs") as batch:
        batch.add_column(
            sa.Column(
                "stage",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'EXTRACT'"),
            )
        )
    op.execute(sa.text("UPDATE ingestion_jobs SET stage = 'EXTRACT' WHERE stage IS NULL"))


def downgrade() -> None:
    _require_sqlite()
    with op.batch_alter_table("ingestion_jobs") as batch:
        batch.drop_column("stage")
    with op.batch_alter_table("doc_versions") as batch:
        batch.drop_column("index_version")
        batch.drop_column("embedding_version")
