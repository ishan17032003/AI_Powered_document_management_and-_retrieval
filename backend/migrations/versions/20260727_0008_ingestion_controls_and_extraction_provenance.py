"""Add ingestion controls and per-version extraction provenance.

Revision ID: 20260727_0008
Revises: 20260727_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0008"
down_revision: str | None = "20260727_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if context.get_context().dialect.name != "sqlite":
        raise RuntimeError("revision 20260727_0008 is approved for SQLite only")


def upgrade() -> None:
    _require_sqlite()

    with op.batch_alter_table("doc_versions") as batch:
        batch.add_column(
            sa.Column("extraction_method", sa.String(length=20), nullable=True)
        )
        batch.add_column(
            sa.Column("extractor_name", sa.String(length=40), nullable=True)
        )
        batch.add_column(sa.Column("ocr_engine", sa.String(length=40), nullable=True))
        batch.add_column(
            sa.Column("ocr_engine_version", sa.String(length=40), nullable=True)
        )
        batch.add_column(
            sa.Column("ocr_languages", sa.String(length=40), nullable=True)
        )
        batch.add_column(
            sa.Column("extraction_quality_score", sa.Float(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "extraction_quality_signals",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column("extraction_completed_at", sa.DateTime(), nullable=True)
        )

    # The job table has no triggers. Recreate it so SQLite can replace the
    # original state check while preserving data, indexes, and foreign keys.
    with op.batch_alter_table("ingestion_jobs", recreate="always") as batch:
        batch.drop_constraint("ck_ingestion_jobs_state", type_="check")
        batch.create_check_constraint(
            "ck_ingestion_jobs_state",
            "state IN "
            "('PENDING', 'RUNNING', 'SUCCEEDED', 'REVIEW', 'FAILED', 'DEAD', "
            "'CANCELLED')",
        )
        batch.create_check_constraint(
            "ck_ingestion_jobs_stage",
            "stage IN ('EXTRACT', 'INDEX')",
        )
        batch.add_column(
            sa.Column(
                "stage_results",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column(
                "degraded_stages",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    _require_sqlite()

    with op.batch_alter_table("ingestion_jobs", recreate="always") as batch:
        batch.drop_constraint("ck_ingestion_jobs_stage", type_="check")
        batch.drop_constraint("ck_ingestion_jobs_state", type_="check")
        batch.create_check_constraint(
            "ck_ingestion_jobs_state",
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'DEAD')",
        )
        batch.drop_column("degraded_stages")
        batch.drop_column("stage_results")

    with op.batch_alter_table("doc_versions") as batch:
        batch.drop_column("extraction_completed_at")
        batch.drop_column("extraction_quality_signals")
        batch.drop_column("extraction_quality_score")
        batch.drop_column("ocr_languages")
        batch.drop_column("ocr_engine_version")
        batch.drop_column("ocr_engine")
        batch.drop_column("extractor_name")
        batch.drop_column("extraction_method")
