"""Data-migration coverage for the MANAGE_KNOWLEDGE capability."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.services import provisioning_service, rbac_service

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
BASELINE_REVISION = "20260727_0001"
HEAD_REVISION = "20260727_0004"
CAPABILITY = "MANAGE_KNOWLEDGE"
DEFAULT_ADMIN_ROLES = {"Super Admin", "Administrator"}
UNCHANGED_TABLES = (
    "assignments",
    "audit_log",
    "cabinets",
    "doc_classes",
    "doc_metadata",
    "doc_versions",
    "documents",
    "dup_groups",
    "dup_members",
    "folders",
    "roles",
    "users",
    "doc_fts",
)


def _config(database: Path) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _current_revision(database: Path) -> str | None:
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _create_snapshot_style_database(
    database: Path,
    *,
    preexisting_capability: bool,
) -> None:
    command.upgrade(_config(database), BASELINE_REVISION)
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(models.Role),
                [
                    {
                        "id": 1,
                        "name": "Super Admin",
                        "description": "default",
                        "is_system": True,
                    },
                    {
                        "id": 2,
                        "name": "Administrator",
                        "description": "default",
                        "is_system": True,
                    },
                    {
                        "id": 3,
                        "name": "Opaque custom bundle",
                        "description": "unrelated",
                        "is_system": False,
                    },
                ],
            )
            permission_rows = [{"id": 1, "code": "CREATE"}]
            if preexisting_capability:
                permission_rows.append({"id": 2, "code": CAPABILITY})
            connection.execute(
                sa.insert(models.Permission),
                permission_rows,
            )
            role_permission_rows = [
                {"id": 1, "role_id": 3, "permission_id": 1},
            ]
            if preexisting_capability:
                role_permission_rows.append({"id": 2, "role_id": 1, "permission_id": 2})
            connection.execute(
                sa.insert(models.RolePermission),
                role_permission_rows,
            )
            connection.execute(
                sa.insert(models.User).values(
                    id=1,
                    username="legacy-user",
                    name="Legacy User",
                    email="legacy-user@example.com",
                    password_hash="non-credential-sentinel",
                    status="active",
                    mfa_enabled=False,
                    created_at=datetime.now(timezone.utc),
                )
            )
            connection.execute(
                sa.insert(models.Assignment).values(
                    id=1,
                    user_id=1,
                    role_id=3,
                    scope_type="GLOBAL",
                    scope_id=None,
                    effect="ALLOW",
                )
            )
    finally:
        engine.dispose()


def _table_rows(database: Path, tables: tuple[str, ...]) -> dict[str, tuple]:
    with sqlite3.connect(database) as connection:
        return {
            table: tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
            for table in tables
        }


def _permission_id(database: Path) -> int | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT id FROM permissions WHERE code = ?",
            (CAPABILITY,),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _unrelated_catalog_rows(database: Path) -> tuple[tuple, tuple]:
    with sqlite3.connect(database) as connection:
        permission_rows = tuple(
            connection.execute(
                """
                SELECT id, code
                FROM permissions
                WHERE code <> ?
                ORDER BY id
                """,
                (CAPABILITY,),
            )
        )
        link_rows = tuple(
            connection.execute(
                """
                SELECT role_permissions.id,
                       role_permissions.role_id,
                       role_permissions.permission_id
                FROM role_permissions
                JOIN permissions
                  ON permissions.id = role_permissions.permission_id
                WHERE permissions.code <> ?
                ORDER BY role_permissions.id
                """,
                (CAPABILITY,),
            )
        )
    return permission_rows, link_rows


def _capability_links(database: Path) -> tuple[str, ...]:
    with sqlite3.connect(database) as connection:
        return tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT roles.name
                FROM role_permissions
                JOIN roles ON roles.id = role_permissions.role_id
                JOIN permissions
                  ON permissions.id = role_permissions.permission_id
                WHERE permissions.code = ?
                ORDER BY roles.name
                """,
                (CAPABILITY,),
            )
        )


def _assert_one_default_link_per_role(database: Path) -> None:
    permission_id = _permission_id(database)
    assert permission_id is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE code = ?",
            (CAPABILITY,),
        ).fetchone() == (1,)
        link_counts = connection.execute(
            """
            SELECT roles.name, COUNT(*)
            FROM role_permissions
            JOIN roles ON roles.id = role_permissions.role_id
            WHERE permission_id = ?
              AND roles.name IN ('Super Admin', 'Administrator')
              AND roles.is_system = 1
            GROUP BY roles.name
            ORDER BY roles.name
            """,
            (permission_id,),
        ).fetchall()
    assert link_counts == [
        ("Administrator", 1),
        ("Super Admin", 1),
    ]


@pytest.mark.parametrize("preexisting_capability", [False, True])
def test_snapshot_upgrade_is_idempotent_and_changes_only_capability_links(
    tmp_path: Path,
    preexisting_capability: bool,
) -> None:
    database = tmp_path / f"snapshot-{preexisting_capability}.db"
    config = _config(database)
    _create_snapshot_style_database(
        database,
        preexisting_capability=preexisting_capability,
    )
    before_unrelated = _table_rows(database, UNCHANGED_TABLES)
    before_unrelated_catalog = _unrelated_catalog_rows(database)

    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")

    assert _current_revision(database) == HEAD_REVISION
    first_permission_id = _permission_id(database)
    assert first_permission_id is not None
    assert set(_capability_links(database)) == DEFAULT_ADMIN_ROLES
    _assert_one_default_link_per_role(database)
    assert _table_rows(database, UNCHANGED_TABLES) == before_unrelated
    assert _unrelated_catalog_rows(database) == before_unrelated_catalog

    # Roll back the later empty ACL schema, then re-adopt the baseline to prove
    # the data migration itself remains idempotent.
    command.downgrade(config, "20260727_0002")
    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")

    assert _permission_id(database) == first_permission_id
    assert _capability_links(database) == (
        "Administrator",
        "Super Admin",
    )
    _assert_one_default_link_per_role(database)
    assert _table_rows(database, UNCHANGED_TABLES) == before_unrelated
    assert _unrelated_catalog_rows(database) == before_unrelated_catalog


def test_empty_head_database_can_be_securely_provisioned(tmp_path: Path) -> None:
    database = tmp_path / "empty-then-provisioned.db"
    config = _config(database)
    command.upgrade(config, "head")

    assert _current_revision(database) == HEAD_REVISION
    assert _permission_id(database) is not None
    assert _capability_links(database) == ()

    engine = create_engine(f"sqlite:///{database}")
    session_factory = sessionmaker(bind=engine)
    session: Session = session_factory()
    try:
        result = provisioning_service.provision_initial_administrator(
            session,
            provisioning_service.InitialAdministrator(
                username="first.operator",
                name="First Operator",
                email="first.operator@example.com",
                password=f"Qz9!{uuid4().hex}{uuid4().hex}",
            ),
        )
        user = session.get(models.User, result.user_id)
        assert user is not None
        assert rbac_service.has_permission(session, user, CAPABILITY)
    finally:
        session.close()
        engine.dispose()

    assert set(_capability_links(database)) == DEFAULT_ADMIN_ROLES
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE code = ?",
            (CAPABILITY,),
        ).fetchone() == (1,)


def test_downgrade_retains_capability_referenced_by_custom_role(
    tmp_path: Path,
) -> None:
    database = tmp_path / "custom-reference.db"
    config = _config(database)
    _create_snapshot_style_database(database, preexisting_capability=False)
    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")

    permission_id = _permission_id(database)
    assert permission_id is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO role_permissions (role_id, permission_id)
            VALUES (?, ?)
            """,
            (3, permission_id),
        )
        connection.commit()

    command.downgrade(config, BASELINE_REVISION)

    assert _current_revision(database) == BASELINE_REVISION
    assert _permission_id(database) == permission_id
    assert set(_capability_links(database)) == {
        *DEFAULT_ADMIN_ROLES,
        "Opaque custom bundle",
    }

    command.upgrade(config, "head")
    assert _current_revision(database) == HEAD_REVISION
    assert set(_capability_links(database)) == {
        *DEFAULT_ADMIN_ROLES,
        "Opaque custom bundle",
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM role_permissions
            WHERE permission_id = ?
            """,
            (permission_id,),
        ).fetchone() == (3,)


def test_downgrade_preserves_catalog_data_and_forward_remains_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unreferenced.db"
    config = _config(database)
    _create_snapshot_style_database(database, preexisting_capability=False)
    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")
    permission_id = _permission_id(database)
    assert permission_id is not None

    command.downgrade(config, BASELINE_REVISION)
    assert _permission_id(database) == permission_id
    assert set(_capability_links(database)) == DEFAULT_ADMIN_ROLES

    command.upgrade(config, "head")
    assert _permission_id(database) == permission_id
    assert set(_capability_links(database)) == DEFAULT_ADMIN_ROLES
