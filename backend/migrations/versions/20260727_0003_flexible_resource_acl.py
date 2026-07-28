"""Add the flexible USER/GROUP resource-ACL schema.

Revision ID: 20260727_0003
Revises: 20260727_0002
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260727_0003"
down_revision: str | None = "20260727_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_sqlite() -> None:
    if context.get_context().dialect.name != "sqlite":
        raise RuntimeError(
            "revision 20260727_0003 implements the approved SQLite ACL schema; "
            "the PostgreSQL ACL migration has not been approved"
        )


def upgrade() -> None:
    _require_sqlite()

    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_groups_id_positive"),
        sa.CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 160",
            name="ck_groups_name_bounded",
        ),
        sa.CheckConstraint(
            "length(description) <= 500",
            name="ck_groups_description_bounded",
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)",
            name="ck_groups_is_active_bool",
        ),
        sa.CheckConstraint(
            "created_by > 0",
            name="ck_groups_creator_positive",
        ),
        sa.UniqueConstraint("name", name="uq_groups_name"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_groups_created_by_users",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_groups_active_name",
        "groups",
        ["is_active", "name"],
        unique=False,
    )

    op.create_table(
        "group_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "id > 0",
            name="ck_group_memberships_id_positive",
        ),
        sa.CheckConstraint(
            "group_id > 0",
            name="ck_group_memberships_group_positive",
        ),
        sa.CheckConstraint(
            "user_id > 0",
            name="ck_group_memberships_user_positive",
        ),
        sa.CheckConstraint(
            "created_by > 0",
            name="ck_group_memberships_creator_positive",
        ),
        sa.UniqueConstraint(
            "group_id",
            "user_id",
            name="uq_group_memberships_group_user",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name="fk_group_memberships_group_id_groups",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_group_memberships_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_group_memberships_created_by_users",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_group_memberships_user_group",
        "group_memberships",
        ["user_id", "group_id"],
        unique=False,
    )

    op.create_table(
        "access_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("principal_type", sa.String(length=5), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("permission_id", sa.Integer(), nullable=False),
        sa.Column("scope_type", sa.String(length=7), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=True),
        sa.Column("effect", sa.String(length=5), nullable=False),
        sa.Column("inherits", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_access_rules_id_positive"),
        sa.CheckConstraint(
            "principal_type IN ('USER', 'GROUP')",
            name="ck_access_rules_principal_type",
        ),
        sa.CheckConstraint(
            """
            (principal_type = 'USER'
             AND user_id IS NOT NULL
             AND user_id > 0
             AND group_id IS NULL)
            OR
            (principal_type = 'GROUP'
             AND group_id IS NOT NULL
             AND group_id > 0
             AND user_id IS NULL)
            """,
            name="ck_access_rules_principal_target",
        ),
        sa.CheckConstraint(
            "permission_id > 0",
            name="ck_access_rules_permission_positive",
        ),
        sa.CheckConstraint(
            "scope_type IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC')",
            name="ck_access_rules_scope_type",
        ),
        sa.CheckConstraint(
            """
            (scope_type = 'GLOBAL' AND scope_id IS NULL)
            OR
            (scope_type IN ('CABINET', 'FOLDER', 'DOC')
             AND scope_id IS NOT NULL
             AND scope_id > 0)
            """,
            name="ck_access_rules_scope_target",
        ),
        sa.CheckConstraint(
            "effect IN ('ALLOW', 'DENY')",
            name="ck_access_rules_effect",
        ),
        sa.CheckConstraint(
            "inherits IN (0, 1)",
            name="ck_access_rules_inherits_bool",
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)",
            name="ck_access_rules_is_active_bool",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR (length(reason) BETWEEN 1 AND 1000 "
            "AND reason = trim(reason))",
            name="ck_access_rules_reason_bounded",
        ),
        sa.CheckConstraint(
            "created_by > 0",
            name="ck_access_rules_creator_positive",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_access_rules_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name="fk_access_rules_group_id_groups",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name="fk_access_rules_permission_id_permissions",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_access_rules_created_by_users",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_access_rules_user_permission_active_expiry",
        "access_rules",
        ["user_id", "permission_id", "is_active", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_access_rules_group_permission_active_expiry",
        "access_rules",
        ["group_id", "permission_id", "is_active", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_access_rules_permission_scope_active_expiry",
        "access_rules",
        [
            "permission_id",
            "scope_type",
            "scope_id",
            "is_active",
            "expires_at",
        ],
        unique=False,
    )

    duplicate_indexes = (
        (
            "uq_acl_user_global_no_expiry",
            ["user_id", "permission_id", "effect", "inherits"],
            """
            is_active = 1
            AND principal_type = 'USER'
            AND scope_type = 'GLOBAL'
            AND expires_at IS NULL
            """,
        ),
        (
            "uq_acl_user_global_with_expiry",
            ["user_id", "permission_id", "effect", "inherits", "expires_at"],
            """
            is_active = 1
            AND principal_type = 'USER'
            AND scope_type = 'GLOBAL'
            AND expires_at IS NOT NULL
            """,
        ),
        (
            "uq_acl_user_resource_no_expiry",
            [
                "user_id",
                "permission_id",
                "scope_type",
                "scope_id",
                "effect",
                "inherits",
            ],
            """
            is_active = 1
            AND principal_type = 'USER'
            AND scope_type <> 'GLOBAL'
            AND expires_at IS NULL
            """,
        ),
        (
            "uq_acl_user_resource_with_expiry",
            [
                "user_id",
                "permission_id",
                "scope_type",
                "scope_id",
                "effect",
                "inherits",
                "expires_at",
            ],
            """
            is_active = 1
            AND principal_type = 'USER'
            AND scope_type <> 'GLOBAL'
            AND expires_at IS NOT NULL
            """,
        ),
        (
            "uq_acl_group_global_no_expiry",
            ["group_id", "permission_id", "effect", "inherits"],
            """
            is_active = 1
            AND principal_type = 'GROUP'
            AND scope_type = 'GLOBAL'
            AND expires_at IS NULL
            """,
        ),
        (
            "uq_acl_group_global_with_expiry",
            ["group_id", "permission_id", "effect", "inherits", "expires_at"],
            """
            is_active = 1
            AND principal_type = 'GROUP'
            AND scope_type = 'GLOBAL'
            AND expires_at IS NOT NULL
            """,
        ),
        (
            "uq_acl_group_resource_no_expiry",
            [
                "group_id",
                "permission_id",
                "scope_type",
                "scope_id",
                "effect",
                "inherits",
            ],
            """
            is_active = 1
            AND principal_type = 'GROUP'
            AND scope_type <> 'GLOBAL'
            AND expires_at IS NULL
            """,
        ),
        (
            "uq_acl_group_resource_with_expiry",
            [
                "group_id",
                "permission_id",
                "scope_type",
                "scope_id",
                "effect",
                "inherits",
                "expires_at",
            ],
            """
            is_active = 1
            AND principal_type = 'GROUP'
            AND scope_type <> 'GLOBAL'
            AND expires_at IS NOT NULL
            """,
        ),
    )
    for index_name, columns, predicate in duplicate_indexes:
        op.create_index(
            index_name,
            "access_rules",
            columns,
            unique=True,
            sqlite_where=sa.text(predicate),
        )

    op.create_table(
        "authorization_policy_state",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "singleton_id = 1",
            name="ck_authorization_policy_state_singleton",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_authorization_policy_state_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "updated_by IS NULL OR updated_by > 0",
            name="ck_authorization_policy_state_updater_positive",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_authorization_policy_state_updated_by_users",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO authorization_policy_state(
                singleton_id,
                revision,
                updated_at,
                updated_by
            )
            VALUES (1, 0, CURRENT_TIMESTAMP, NULL)
            """
        )
    )


def downgrade() -> None:
    _require_sqlite()

    op.drop_table("authorization_policy_state")

    for index_name in (
        "uq_acl_group_resource_with_expiry",
        "uq_acl_group_resource_no_expiry",
        "uq_acl_group_global_with_expiry",
        "uq_acl_group_global_no_expiry",
        "uq_acl_user_resource_with_expiry",
        "uq_acl_user_resource_no_expiry",
        "uq_acl_user_global_with_expiry",
        "uq_acl_user_global_no_expiry",
        "ix_access_rules_permission_scope_active_expiry",
        "ix_access_rules_group_permission_active_expiry",
        "ix_access_rules_user_permission_active_expiry",
    ):
        op.drop_index(index_name, table_name="access_rules")
    op.drop_table("access_rules")

    op.drop_index(
        "ix_group_memberships_user_group",
        table_name="group_memberships",
    )
    op.drop_table("group_memberships")

    op.drop_index("ix_groups_active_name", table_name="groups")
    op.drop_table("groups")
