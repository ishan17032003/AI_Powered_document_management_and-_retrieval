"""Add durable job, outbox, session, and document lifecycle state.

Revision ID: 20260727_0004
Revises: 20260727_0003
Create Date: 2026-07-27

This revision is intentionally limited to the validated SQLite profile.  It
adds persistence primitives used by later ingestion, authentication, and
audit work; no worker, queue, or token code is enabled by this migration.
Existing document/version rows are backfilled with explicit, conservative
states and the checked-in source database is never opened by the migration
tests.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0004"
down_revision: str | None = "20260727_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if context.get_context().dialect.name != "sqlite":
        raise RuntimeError(
            "revision 20260727_0004 implements the approved SQLite jobs and "
            "lifecycle schema; the PostgreSQL profile has not been approved"
        )


def upgrade() -> None:
    _require_sqlite()

    # Existing rows are known to have a single, usable version.  Additive
    # columns retain a database default so old writers can continue during a
    # rolling deployment; the ORM also declares the same server defaults.
    op.add_column(
        "documents",
        sa.Column(
            "lifecycle_state",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'ACTIVE'"),
        ),
    )
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("failure_code", sa.String(length=80), nullable=True),
    )
    op.create_index(
        "ix_documents_folder_lifecycle_created",
        "documents",
        ["folder_id", "lifecycle_state", "created_at"],
        unique=False,
    )

    op.add_column(
        "doc_versions",
        sa.Column(
            "storage_state",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'AVAILABLE'"),
        ),
    )
    op.add_column(
        "doc_versions",
        sa.Column(
            "extractor_version",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'legacy-v1'"),
        ),
    )
    op.add_column(
        "doc_versions",
        sa.Column(
            "chunker_version",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'legacy-v1'"),
        ),
    )
    op.create_index(
        "uq_doc_versions_document_version",
        "doc_versions",
        ["document_id", "version_no"],
        unique=True,
    )
    op.create_index(
        "ix_doc_versions_storage_state",
        "doc_versions",
        ["storage_state", "document_id"],
        unique=False,
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column(
            "stage_version",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("lock_owner", sa.String(length=160), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "length(id) BETWEEN 1 AND 36",
            name="ck_ingestion_jobs_id_bounded",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'DEAD')",
            name="ck_ingestion_jobs_state",
        ),
        sa.CheckConstraint(
            "length(stage_version) BETWEEN 1 AND 40",
            name="ck_ingestion_jobs_stage_version_bounded",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200",
            name="ck_ingestion_jobs_idempotency_bounded",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_ingestion_jobs_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND 80",
            name="ck_ingestion_jobs_error_code_bounded",
        ),
        sa.CheckConstraint(
            "error_message IS NULL OR length(error_message) BETWEEN 1 AND 500",
            name="ck_ingestion_jobs_error_message_bounded",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_ingestion_jobs_idempotency"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_ingestion_jobs_document_id_documents",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["doc_versions.id"],
            name="fk_ingestion_jobs_version_id_doc_versions",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingestion_jobs_state_available",
        "ingestion_jobs",
        ["state", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_ingestion_jobs_document_state",
        "ingestion_jobs",
        ["document_id", "state"],
        unique=False,
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("aggregate_type", sa.String(length=40), nullable=False),
        sa.Column("aggregate_id", sa.String(length=80), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "state",
            sa.String(length=12),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lock_owner", sa.String(length=160), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("dead_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("last_error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(id) BETWEEN 1 AND 36",
            name="ck_outbox_events_id_bounded",
        ),
        sa.CheckConstraint(
            "length(aggregate_type) BETWEEN 1 AND 40",
            name="ck_outbox_events_aggregate_type_bounded",
        ),
        sa.CheckConstraint(
            "length(aggregate_id) BETWEEN 1 AND 80",
            name="ck_outbox_events_aggregate_id_bounded",
        ),
        sa.CheckConstraint(
            "length(event_type) BETWEEN 1 AND 80",
            name="ck_outbox_events_event_type_bounded",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_outbox_events_schema_version_positive",
        ),
        sa.CheckConstraint(
            "length(payload) BETWEEN 2 AND 1048576",
            name="ck_outbox_events_payload_bounded",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200",
            name="ck_outbox_events_idempotency_bounded",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'CLAIMED', 'PROCESSED', 'DEAD')",
            name="ck_outbox_events_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_events_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 80",
            name="ck_outbox_events_error_code_bounded",
        ),
        sa.CheckConstraint(
            "last_error_message IS NULL OR length(last_error_message) BETWEEN 1 AND 500",
            name="ck_outbox_events_error_message_bounded",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_state_available",
        "outbox_events",
        ["state", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("refresh_secret_hash", sa.String(length=255), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "length(id) BETWEEN 1 AND 128",
            name="ck_auth_sessions_id_bounded",
        ),
        sa.CheckConstraint(
            "token_version >= 0",
            name="ck_auth_sessions_token_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_auth_sessions_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_sessions_user_expiry",
        "auth_sessions",
        ["user_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_auth_sessions_revoked_expiry",
        "auth_sessions",
        ["revoked_at", "expires_at"],
        unique=False,
    )

    op.create_table(
        "auth_token_revocations",
        sa.Column("jti", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=False),
        sa.CheckConstraint(
            "length(jti) BETWEEN 1 AND 128",
            name="ck_auth_token_revocations_jti_bounded",
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 120",
            name="ck_auth_token_revocations_reason_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_auth_token_revocations_user_id_users",
        ),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(
        "ix_auth_token_revocations_expiry",
        "auth_token_revocations",
        ["expires_at"],
        unique=False,
    )

    # Guard state transitions and immutable object metadata at the database
    # boundary.  Triggers are deliberately small and portable within the
    # approved SQLite profile; later PostgreSQL work will provide equivalents.
    op.execute(
        """
        CREATE TRIGGER ck_documents_lifecycle_state_insert
        BEFORE INSERT ON documents
        WHEN NEW.lifecycle_state NOT IN ('ACTIVE', 'TOMBSTONED', 'DELETED')
        BEGIN SELECT RAISE(ABORT, 'invalid document lifecycle state'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER ck_documents_lifecycle_state_update
        BEFORE UPDATE OF lifecycle_state ON documents
        WHEN NEW.lifecycle_state NOT IN ('ACTIVE', 'TOMBSTONED', 'DELETED')
        BEGIN SELECT RAISE(ABORT, 'invalid document lifecycle state'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER ck_doc_versions_storage_state_insert
        BEFORE INSERT ON doc_versions
        WHEN NEW.storage_state NOT IN ('STAGED', 'AVAILABLE', 'QUARANTINED', 'MISSING', 'DELETED')
        BEGIN SELECT RAISE(ABORT, 'invalid document version storage state'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER ck_doc_versions_storage_state_update
        BEFORE UPDATE OF storage_state ON doc_versions
        WHEN NEW.storage_state NOT IN ('STAGED', 'AVAILABLE', 'QUARANTINED', 'MISSING', 'DELETED')
        BEGIN SELECT RAISE(ABORT, 'invalid document version storage state'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER ck_doc_versions_immutable_metadata
        BEFORE UPDATE OF file_key, size, checksum ON doc_versions
        WHEN NEW.file_key <> OLD.file_key OR NEW.size <> OLD.size OR NEW.checksum <> OLD.checksum
        BEGIN SELECT RAISE(ABORT, 'document version metadata is immutable'); END
        """
    )


def downgrade() -> None:
    _require_sqlite()

    for trigger_name in (
        "ck_doc_versions_immutable_metadata",
        "ck_doc_versions_storage_state_update",
        "ck_doc_versions_storage_state_insert",
        "ck_documents_lifecycle_state_update",
        "ck_documents_lifecycle_state_insert",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))

    op.drop_index(
        "ix_auth_token_revocations_expiry", table_name="auth_token_revocations"
    )
    op.drop_table("auth_token_revocations")
    op.drop_index("ix_auth_sessions_revoked_expiry", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_expiry", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_state_available", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_ingestion_jobs_document_state", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_state_available", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_doc_versions_storage_state", table_name="doc_versions")
    op.drop_index("uq_doc_versions_document_version", table_name="doc_versions")
    op.drop_column("doc_versions", "chunker_version")
    op.drop_column("doc_versions", "extractor_version")
    op.drop_column("doc_versions", "storage_state")
    op.drop_index("ix_documents_folder_lifecycle_created", table_name="documents")
    op.drop_column("documents", "failure_code")
    op.drop_column("documents", "deleted_at")
    op.drop_column("documents", "lifecycle_state")
