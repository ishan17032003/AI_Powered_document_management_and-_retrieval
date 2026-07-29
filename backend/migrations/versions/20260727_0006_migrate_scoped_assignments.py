"""Migrate legacy scoped role assignments into permission-level ACL rules.

Revision ID: 20260727_0006
Revises: 20260727_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0006"
down_revision: str | None = "20260727_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if False:
        raise RuntimeError("revision 20260727_0006 is approved for SQLite only")


def _preflight() -> None:
    bind = op.get_bind()
    checks = (
        ("assignments", "scope_type NOT IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC') OR effect NOT IN ('ALLOW', 'DENY')"),
        ("assignments", "(scope_type = 'GLOBAL' AND scope_id IS NOT NULL) OR (scope_type <> 'GLOBAL' AND scope_id IS NULL)"),
        ("role_permissions", "permission_id <= 0"),
    )
    for table, predicate in checks:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {predicate}")).scalar_one()
        if count:
            raise RuntimeError(f"MIG-008 preflight conflict: {table} has {count} invalid row(s)")


def upgrade() -> None:
    _require_sqlite()
    _preflight()
    op.create_table(
        "acl_migration_report",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission_code", sa.String(length=40), nullable=False),
        sa.Column("scope_type", sa.String(length=12), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=True),
        sa.Column("before_effect", sa.String(length=6), nullable=False),
        sa.Column("after_rule_id", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
        sa.ForeignKeyConstraint(["after_rule_id"], ["access_rules.id"]),
    )
    op.create_index("ix_acl_migration_report_assignment", "acl_migration_report", ["assignment_id"])
    op.create_index("ix_acl_migration_report_outcome", "acl_migration_report", ["outcome"])

    is_sqlite = op.get_context().dialect.name == "sqlite"
    bool_true = "1" if is_sqlite else "TRUE"
    bool_false = "0" if is_sqlite else "FALSE"
    op.execute(sa.text(f"""
        INSERT INTO access_rules(
            principal_type, user_id, group_id, permission_id, scope_type, scope_id,
            effect, inherits, is_active, expires_at, reason, created_by, created_at, updated_at
        )
        SELECT 'USER', a.user_id, NULL, rp.permission_id, a.scope_type, a.scope_id,
               a.effect, CASE WHEN a.scope_type = 'DOC' THEN {bool_false} ELSE {bool_true} END,
               {bool_true}, NULL, 'MIG-008 legacy role assignment', a.user_id,
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM assignments a
        JOIN role_permissions rp ON rp.role_id = a.role_id
        WHERE NOT EXISTS (
            SELECT 1 FROM access_rules ar
            WHERE ar.principal_type = 'USER'
              AND ar.user_id = a.user_id
              AND ar.permission_id = rp.permission_id
              AND ar.scope_type = a.scope_type
              AND (ar.scope_id = a.scope_id OR (ar.scope_id IS NULL AND a.scope_id IS NULL))
              AND ar.effect = a.effect
              AND ar.inherits = CASE WHEN a.scope_type = 'DOC' THEN {bool_false} ELSE {bool_true} END
              AND ar.is_active = {bool_true}
              AND ar.expires_at IS NULL
        )
    """))
    op.execute(sa.text(f"""
        INSERT INTO acl_migration_report(
            assignment_id, user_id, role_id, permission_code, scope_type, scope_id,
            before_effect, after_rule_id, outcome
        )
        SELECT a.id, a.user_id, a.role_id, p.code, a.scope_type, a.scope_id,
               a.effect, ar.id, 'PRESERVED'
        FROM assignments a
        JOIN role_permissions rp ON rp.role_id = a.role_id
        JOIN permissions p ON p.id = rp.permission_id
        LEFT JOIN access_rules ar
          ON ar.principal_type = 'USER'
         AND ar.user_id = a.user_id
         AND ar.permission_id = rp.permission_id
         AND ar.scope_type = a.scope_type
         AND (ar.scope_id = a.scope_id OR (ar.scope_id IS NULL AND a.scope_id IS NULL))
         AND ar.effect = a.effect
         AND ar.inherits = CASE WHEN a.scope_type = 'DOC' THEN {bool_false} ELSE {bool_true} END
         AND ar.is_active = {bool_true}
         AND ar.expires_at IS NULL
    """))
    op.execute(sa.text("""
        UPDATE authorization_policy_state
        SET revision = revision + CASE WHEN EXISTS (SELECT 1 FROM acl_migration_report) THEN 1 ELSE 0 END,
            updated_at = CURRENT_TIMESTAMP
        WHERE singleton_id = 1
    """))


def downgrade() -> None:
    _require_sqlite()
    op.execute(sa.text("DELETE FROM access_rules WHERE reason = 'MIG-008 legacy role assignment'"))
    op.drop_index("ix_acl_migration_report_outcome", table_name="acl_migration_report")
    op.drop_index("ix_acl_migration_report_assignment", table_name="acl_migration_report")
    op.drop_table("acl_migration_report")
