"""Add retention/legal-hold fields for transactional document deletion.

Revision ID: 20260727_0010
Revises: 20260727_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0010"
down_revision: str | None = "20260727_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if context.get_context().dialect.name != "sqlite":
        raise RuntimeError("revision 20260727_0009 is approved for SQLite only")


def upgrade() -> None:
    _require_sqlite()
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("retention_until", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.text("0")))
        batch.add_column(sa.Column("legal_hold_reason", sa.String(length=200), nullable=True))
    op.create_index("ix_documents_retention_hold", "documents", ["retention_until", "legal_hold", "lifecycle_state"])


def downgrade() -> None:
    _require_sqlite()
    op.drop_index("ix_documents_retention_hold", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("legal_hold_reason")
        batch.drop_column("legal_hold")
        batch.drop_column("retention_until")
