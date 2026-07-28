"""Apply post-preflight integrity constraints and operational indexes.

Revision ID: 20260727_0005
Revises: 20260727_0004

The preflight runs before any DDL and reports the first conflicting table. This
keeps an upgrade fail-safe for databases that were not produced by the checked
in migration chain.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0005"
down_revision: str | None = "20260727_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if context.get_context().dialect.name != "sqlite":
        raise RuntimeError("revision 20260727_0005 is approved for SQLite only")


def _preflight() -> None:
    checks = (
        ("users", "status NOT IN ('active', 'suspended') OR mfa_enabled NOT IN (0, 1)"),
        ("assignments", "scope_type NOT IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC') OR effect NOT IN ('ALLOW', 'DENY') OR (scope_type = 'GLOBAL' AND scope_id IS NOT NULL) OR (scope_type <> 'GLOBAL' AND (scope_id IS NULL OR scope_id <= 0))"),
        ("documents", "status NOT IN ('PROCESSING', 'READY', 'REVIEW', 'ERROR') OR ocr_status NOT IN ('pending', 'native', 'ocr', 'unavailable', 'skipped', 'error') OR page_count < 0"),
        ("doc_versions", "version_no <= 0 OR size < 0"),
    )
    bind = op.get_bind()
    for table, predicate in checks:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {predicate}")).scalar_one()
        if count:
            raise RuntimeError(f"MIG-004 preflight conflict: {table} has {count} invalid row(s)")


def _trigger(name: str, table: str, event: str, predicate: str, message: str) -> None:
    op.execute(sa.text(f"""
        CREATE TRIGGER {name}
        BEFORE {event} ON {table}
        WHEN {predicate}
        BEGIN SELECT RAISE(ABORT, '{message}'); END
    """))


def upgrade() -> None:
    _require_sqlite()
    _preflight()
    _trigger("ck_users_status_insert", "users", "INSERT", "NEW.status NOT IN ('active', 'suspended')", "invalid user status")
    _trigger("ck_users_status_update", "users", "UPDATE OF status", "NEW.status NOT IN ('active', 'suspended')", "invalid user status")
    _trigger("ck_assignments_values_insert", "assignments", "INSERT", "NEW.scope_type NOT IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC') OR NEW.effect NOT IN ('ALLOW', 'DENY') OR (NEW.scope_type = 'GLOBAL' AND NEW.scope_id IS NOT NULL) OR (NEW.scope_type <> 'GLOBAL' AND (NEW.scope_id IS NULL OR NEW.scope_id <= 0))", "invalid assignment scope or effect")
    _trigger("ck_assignments_values_update", "assignments", "UPDATE", "NEW.scope_type NOT IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC') OR NEW.effect NOT IN ('ALLOW', 'DENY') OR (NEW.scope_type = 'GLOBAL' AND NEW.scope_id IS NOT NULL) OR (NEW.scope_type <> 'GLOBAL' AND (NEW.scope_id IS NULL OR NEW.scope_id <= 0))", "invalid assignment scope or effect")
    _trigger("ck_documents_values_insert", "documents", "INSERT", "NEW.status NOT IN ('PROCESSING', 'READY', 'REVIEW', 'ERROR') OR NEW.ocr_status NOT IN ('pending', 'native', 'ocr', 'unavailable', 'skipped', 'error') OR NEW.page_count < 0", "invalid document state")
    _trigger("ck_documents_values_update", "documents", "UPDATE", "NEW.status NOT IN ('PROCESSING', 'READY', 'REVIEW', 'ERROR') OR NEW.ocr_status NOT IN ('pending', 'native', 'ocr', 'unavailable', 'skipped', 'error') OR NEW.page_count < 0", "invalid document state")
    _trigger("ck_doc_versions_values_insert", "doc_versions", "INSERT", "NEW.version_no <= 0 OR NEW.size < 0", "invalid document version values")
    _trigger("ck_doc_versions_values_update", "doc_versions", "UPDATE", "NEW.version_no <= 0 OR NEW.size < 0", "invalid document version values")
    op.create_index("ix_users_status_created", "users", ["status", "created_at"])
    op.create_index("ix_assignments_user_scope", "assignments", ["user_id", "scope_type", "scope_id"])
    op.create_index("ix_assignments_role_scope", "assignments", ["role_id", "scope_type", "scope_id"])
    op.create_index("ix_doc_versions_document_created", "doc_versions", ["document_id", "created_at"])
    op.create_index("ix_audit_log_actor_timestamp", "audit_log", ["actor_id", "timestamp"])


def downgrade() -> None:
    _require_sqlite()
    for name in ("ix_audit_log_actor_timestamp", "ix_doc_versions_document_created", "ix_assignments_role_scope", "ix_assignments_user_scope", "ix_users_status_created"):
        op.drop_index(name)
    for name in ("ck_doc_versions_values_update", "ck_doc_versions_values_insert", "ck_documents_values_update", "ck_documents_values_insert", "ck_assignments_values_update", "ck_assignments_values_insert", "ck_users_status_update", "ck_users_status_insert"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
