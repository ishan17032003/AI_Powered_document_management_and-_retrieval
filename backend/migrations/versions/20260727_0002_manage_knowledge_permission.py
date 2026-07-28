"""Add the shared-knowledge management capability to default admin bundles.

Revision ID: 20260727_0002
Revises: 20260727_0001
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0002"
down_revision: str | None = "20260727_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "MANAGE_KNOWLEDGE"
_DEFAULT_ADMIN_ROLES = ("Super Admin", "Administrator")

permissions = sa.table(
    "permissions",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
)
roles = sa.table(
    "roles",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("is_system", sa.Boolean),
)
role_permissions = sa.table(
    "role_permissions",
    sa.column("id", sa.Integer),
    sa.column("role_id", sa.Integer),
    sa.column("permission_id", sa.Integer),
)


def _permission_id(connection: sa.Connection) -> int | None:
    return connection.execute(
        sa.select(permissions.c.id).where(permissions.c.code == _PERMISSION_CODE)
    ).scalar_one_or_none()


def _default_admin_role_ids(connection: sa.Connection) -> tuple[int, ...]:
    return tuple(
        connection.execute(
            sa.select(roles.c.id).where(
                roles.c.is_system.is_(True),
                roles.c.name.in_(_DEFAULT_ADMIN_ROLES),
            )
        ).scalars()
    )


def upgrade() -> None:
    connection = op.get_bind()
    permission_id = _permission_id(connection)
    if permission_id is None:
        connection.execute(permissions.insert().values(code=_PERMISSION_CODE))
        permission_id = _permission_id(connection)
    if permission_id is None:
        raise RuntimeError("MANAGE_KNOWLEDGE permission could not be created")

    for role_id in _default_admin_role_ids(connection):
        existing_link = connection.execute(
            sa.select(role_permissions.c.id).where(
                role_permissions.c.role_id == role_id,
                role_permissions.c.permission_id == permission_id,
            )
        ).scalar_one_or_none()
        if existing_link is None:
            connection.execute(
                role_permissions.insert().values(
                    role_id=role_id,
                    permission_id=permission_id,
                )
            )


def downgrade() -> None:
    """Retain catalog data because its provenance cannot be reconstructed safely.

    A permission or link may have existed before this revision was applied. Older
    application code ignores this additional capability, so preserving the rows is
    backward-compatible and avoids deleting operator-managed or custom-role data.
    Re-upgrade is idempotent.
    """
