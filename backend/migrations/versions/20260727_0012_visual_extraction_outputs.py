"""Add versioned, explicitly untrusted visual OCR/caption outputs.

Revision ID: 20260727_0012
Revises: 20260727_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0012"
down_revision: str | None = "20260727_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if False:
        raise RuntimeError("revision 20260727_0012 is approved for SQLite only")


def upgrade() -> None:
    _require_sqlite()
    is_sqlite = op.get_context().dialect.name == "sqlite"
    op.create_table(
        "visual_extractions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("visual_assets.id"), nullable=False),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("doc_versions.id"), nullable=False),
        sa.Column("output_type", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("engine_revision", sa.String(length=160), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("language", sa.String(length=40), nullable=True),
        sa.Column("quality_signals", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.text("0") if is_sqlite else sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("output_type IN ('OCR', 'CAPTION', 'DESCRIPTION')", name="ck_visual_extractions_type"),
        sa.CheckConstraint(f"trusted {'IN (0, 1)' if is_sqlite else 'IN (true, false)'}", name="ck_visual_extractions_untrusted_bool"),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_visual_extractions_confidence"),
        sa.UniqueConstraint("asset_id", "output_type", "engine_revision", name="uq_visual_extractions_revision"),
    )
    op.create_index("ix_visual_extractions_version_type", "visual_extractions", ["version_id", "output_type"])


def downgrade() -> None:
    _require_sqlite()
    op.drop_index("ix_visual_extractions_version_type", table_name="visual_extractions")
    op.drop_table("visual_extractions")
