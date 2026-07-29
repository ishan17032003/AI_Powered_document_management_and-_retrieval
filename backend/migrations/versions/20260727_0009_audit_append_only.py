"""Protect the audit log from mutation.

The SQLite profile has no separate database roles, so append-only semantics are
enforced at the database boundary with aborting triggers.  PostgreSQL deploys
should use a dedicated owner/writer role and equivalent privileges; this
migration deliberately fails closed on unsupported dialects rather than
pretending to provide those guarantees.
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "20260727_0009"
down_revision: str | None = "20260727_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if False:
        raise RuntimeError("revision 20260727_0009 is approved for SQLite only")


def upgrade() -> None:
    _require_sqlite()
    is_sqlite = op.get_context().dialect.name == "sqlite"
    if is_sqlite:
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_log_append_only_update
            BEFORE UPDATE ON audit_log
            BEGIN
                SELECT RAISE(ABORT, 'audit_log_append_only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_log_append_only_delete
            BEFORE DELETE ON audit_log
            BEGIN
                SELECT RAISE(ABORT, 'audit_log_append_only');
            END
            """
        )


def downgrade() -> None:
    _require_sqlite()
    is_sqlite = op.get_context().dialect.name == "sqlite"
    if is_sqlite:
        op.execute("DROP TRIGGER IF EXISTS audit_log_append_only_update")
        op.execute("DROP TRIGGER IF EXISTS audit_log_append_only_delete")
