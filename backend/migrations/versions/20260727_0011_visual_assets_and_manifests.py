"""Add visual assets, explicit lineage, processing state, and retrieval manifests.

Revision ID: 20260727_0011
Revises: 20260727_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0011"
down_revision: str | None = "20260727_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if False:
        raise RuntimeError("revision 20260727_0011 is approved for SQLite only")


def upgrade() -> None:
    _require_sqlite()
    op.create_table(
        "visual_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_key", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("doc_versions.id"), nullable=False),
        sa.Column("asset_type", sa.String(length=12), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("source_asset_id", sa.Integer(), sa.ForeignKey("visual_assets.id"), nullable=True),
        sa.Column("file_key", sa.String(length=300), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("perceptual_hash", sa.String(length=128), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lifecycle_state", sa.String(length=16), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("asset_type IN ('PAGE', 'IMAGE', 'REGION', 'THUMBNAIL')", name="ck_visual_assets_type"),
        sa.CheckConstraint("lifecycle_state IN ('ACTIVE', 'SUPERSEDED', 'TOMBSTONED', 'DELETED', 'QUARANTINED')", name="ck_visual_assets_lifecycle"),
        sa.CheckConstraint("width IS NULL OR width > 0", name="ck_visual_assets_width"),
        sa.CheckConstraint("height IS NULL OR height > 0", name="ck_visual_assets_height"),
        sa.CheckConstraint("size >= 0", name="ck_visual_assets_size_nonnegative"),
        sa.UniqueConstraint("asset_key", name="uq_visual_assets_asset_key"),
    )
    op.create_index("ix_visual_assets_version_state", "visual_assets", ["version_id", "lifecycle_state"])
    op.create_index("ix_visual_assets_document_page", "visual_assets", ["document_id", "page_number"])

    op.create_table(
        "visual_asset_lineage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("visual_assets.id"), nullable=False),
        sa.Column("source_asset_id", sa.Integer(), sa.ForeignKey("visual_assets.id"), nullable=False),
        sa.Column("relationship_type", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("relationship_type IN ('DERIVED_FROM', 'RENDERED_FROM', 'THUMBNAIL_OF', 'REGION_OF')", name="ck_visual_asset_lineage_type"),
        sa.UniqueConstraint("asset_id", "source_asset_id", "relationship_type", name="uq_visual_asset_lineage_edge"),
    )
    op.create_index("ix_visual_asset_lineage_source", "visual_asset_lineage", ["source_asset_id"])

    op.create_table(
        "visual_processing_manifests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("doc_versions.id"), nullable=False),
        sa.Column("manifest_version", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("extractor_revision", sa.String(length=80), nullable=False),
        sa.Column("derivative_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("stage", sa.String(length=16), nullable=False, server_default=sa.text("'VALIDATE'")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('PENDING', 'RUNNING', 'READY', 'FAILED', 'SUPERSEDED', 'DELETED')", name="ck_visual_processing_manifest_state"),
        sa.CheckConstraint("stage IN ('VALIDATE', 'EXTRACT', 'DERIVE', 'PERSIST')", name="ck_visual_processing_manifest_stage"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_visual_processing_manifest_attempts"),
        sa.UniqueConstraint("version_id", "manifest_version", name="uq_visual_processing_manifest_version"),
    )
    op.create_index("ix_visual_processing_manifest_state", "visual_processing_manifests", ["state", "updated_at"])
    op.create_index("ix_visual_processing_manifest_claim", "visual_processing_manifests", ["state", "next_attempt_at"])

    op.create_table(
        "visual_retrieval_manifests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lane", sa.String(length=20), nullable=False),
        sa.Column("manifest_version", sa.String(length=80), nullable=False),
        sa.Column("model_revision", sa.String(length=160), nullable=False),
        sa.Column("model_sha256", sa.String(length=64), nullable=False),
        sa.Column("vector_dimension", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False, server_default=sa.text("'BUILDING'")),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("state IN ('BUILDING', 'READY', 'CANARY', 'RETIRED', 'FAILED')", name="ck_visual_retrieval_manifest_state"),
        sa.UniqueConstraint("lane", "manifest_version", name="uq_visual_retrieval_manifest_version"),
    )
    op.create_index("ix_visual_retrieval_manifest_lane_state", "visual_retrieval_manifests", ["lane", "state"])


def downgrade() -> None:
    _require_sqlite()
    op.drop_index("ix_visual_retrieval_manifest_lane_state", table_name="visual_retrieval_manifests")
    op.drop_table("visual_retrieval_manifests")
    op.drop_index("ix_visual_processing_manifest_state", table_name="visual_processing_manifests")
    op.drop_index("ix_visual_processing_manifest_claim", table_name="visual_processing_manifests")
    op.drop_table("visual_processing_manifests")
    op.drop_index("ix_visual_asset_lineage_source", table_name="visual_asset_lineage")
    op.drop_table("visual_asset_lineage")
    op.drop_index("ix_visual_assets_document_page", table_name="visual_assets")
    op.drop_index("ix_visual_assets_version_state", table_name="visual_assets")
    op.drop_table("visual_assets")
