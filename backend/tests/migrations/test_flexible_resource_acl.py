"""Migration proofs for the SQLite flexible resource-ACL schema."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
SOURCE_DATABASE = BACKEND_DIR / "docvault.db"
PREVIOUS_REVISION = "20260727_0002"
HEAD_REVISION = "20260727_0003"
ACL_TABLES = (
    "access_rules",
    "authorization_policy_state",
    "group_memberships",
    "groups",
)
LEGACY_TABLES = (
    "assignments",
    "audit_log",
    "cabinets",
    "doc_classes",
    "doc_fts",
    "doc_metadata",
    "doc_versions",
    "documents",
    "dup_groups",
    "dup_members",
    "folders",
    "permissions",
    "role_permissions",
    "roles",
    "users",
)
NOW = "2026-07-27 12:00:00"
LATER = "2026-08-01 12:00:00"

ACCESS_RULE_INSERT = """
    INSERT INTO access_rules(
        id,
        principal_type,
        user_id,
        group_id,
        permission_id,
        scope_type,
        scope_id,
        effect,
        inherits,
        is_active,
        expires_at,
        reason,
        created_by,
        created_at,
        updated_at
    )
    VALUES (
        :id,
        :principal_type,
        :user_id,
        :group_id,
        :permission_id,
        :scope_type,
        :scope_id,
        :effect,
        :inherits,
        :is_active,
        :expires_at,
        :reason,
        :created_by,
        :created_at,
        :updated_at
    )
"""


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


def _copy_current_snapshot(destination: Path) -> None:
    """Copy source bytes without opening the protected checked-in database."""
    assert SOURCE_DATABASE.is_file()
    shutil.copy2(SOURCE_DATABASE, destination)
    for suffix in ("-wal", "-shm"):
        source_sidecar = Path(f"{SOURCE_DATABASE}{suffix}")
        if source_sidecar.is_file():
            shutil.copy2(source_sidecar, Path(f"{destination}{suffix}"))

    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _table_rows(
    database: Path,
    table_names: tuple[str, ...] = LEGACY_TABLES,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        return {
            table_name: tuple(
                connection.execute(f'SELECT * FROM "{table_name}" ORDER BY rowid')
            )
            for table_name in table_names
        }


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    return connection


def _seed_acl_parents(database: Path) -> None:
    with _connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO users(
                id,
                username,
                name,
                email,
                password_hash,
                status,
                mfa_enabled,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'active', 0, ?)
            """,
            (
                (
                    1,
                    "acl-owner",
                    "ACL Owner",
                    "acl-owner@example.test",
                    "non-credential-test-sentinel",
                    NOW,
                ),
                (
                    2,
                    "acl-member",
                    "ACL Member",
                    "acl-member@example.test",
                    "non-credential-test-sentinel",
                    NOW,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO permissions(id, code) VALUES (100, 'ACL_TEST_VIEW')"
        )
        connection.execute(
            """
            INSERT INTO groups(
                id,
                name,
                description,
                is_active,
                created_by,
                created_at,
                updated_at
            )
            VALUES (1, 'Reviewers', 'Migration test group', 1, 1, ?, ?)
            """,
            (NOW, NOW),
        )


def _rule(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": 1,
        "principal_type": "USER",
        "user_id": 2,
        "group_id": None,
        "permission_id": 100,
        "scope_type": "GLOBAL",
        "scope_id": None,
        "effect": "ALLOW",
        "inherits": 1,
        "is_active": 1,
        "expires_at": None,
        "reason": "Approved migration test rule",
        "created_by": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


def _assert_integrity_error(
    database: Path,
    statement: str,
    parameters: tuple[object, ...] | dict[str, object],
) -> None:
    with _connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, parameters)


@pytest.fixture
def acl_database(tmp_path: Path) -> Iterator[Path]:
    database = tmp_path / "acl.db"
    command.upgrade(_config(database), HEAD_REVISION)
    _seed_acl_parents(database)
    yield database


def test_empty_upgrade_has_exact_acl_shape_and_singleton_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "empty.db"
    config = _config(database)
    command.upgrade(config, HEAD_REVISION)

    assert _current_revision(database) == HEAD_REVISION
    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        assert set(ACL_TABLES).issubset(inspector.get_table_names())
        assert {column["name"] for column in inspector.get_columns("access_rules")} == {
            "id",
            "principal_type",
            "user_id",
            "group_id",
            "permission_id",
            "scope_type",
            "scope_id",
            "effect",
            "inherits",
            "is_active",
            "expires_at",
            "reason",
            "created_by",
            "created_at",
            "updated_at",
        }
        access_rule_foreign_keys = {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
            )
            for foreign_key in inspector.get_foreign_keys("access_rules")
        }
        assert access_rule_foreign_keys == {
            (("created_by",), "users"),
            (("group_id",), "groups"),
            (("permission_id",), "permissions"),
            (("user_id",), "users"),
        }
        index_names = {index["name"] for index in inspector.get_indexes("access_rules")}
        assert {
            "ix_access_rules_user_permission_active_expiry",
            "ix_access_rules_group_permission_active_expiry",
            "ix_access_rules_permission_scope_active_expiry",
            "uq_acl_user_global_no_expiry",
            "uq_acl_user_global_with_expiry",
            "uq_acl_user_resource_no_expiry",
            "uq_acl_user_resource_with_expiry",
            "uq_acl_group_global_no_expiry",
            "uq_acl_group_global_with_expiry",
            "uq_acl_group_resource_no_expiry",
            "uq_acl_group_resource_with_expiry",
        } == index_names
        assert {
            index["name"] for index in inspector.get_indexes("group_memberships")
        } == {"ix_group_memberships_user_group"}
        assert {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("group_memberships")
        } == {("group_id", "user_id")}
    finally:
        engine.dispose()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM groups").fetchall() == []
        assert connection.execute("SELECT * FROM group_memberships").fetchall() == []
        assert connection.execute("SELECT * FROM access_rules").fetchall() == []
        assert connection.execute(
            """
            SELECT singleton_id, revision, updated_by
            FROM authorization_policy_state
            """
        ).fetchall() == [(1, 0, None)]


def test_adopted_current_snapshot_upgrade_preserves_every_existing_row(
    tmp_path: Path,
) -> None:
    database = tmp_path / "adopted-current.db"
    _copy_current_snapshot(database)
    config = _config(database)

    command.stamp(config, "20260727_0001")
    command.upgrade(config, PREVIOUS_REVISION)
    before = _table_rows(database)

    command.upgrade(config, HEAD_REVISION)

    assert _current_revision(database) == HEAD_REVISION
    assert _table_rows(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM groups").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM group_memberships"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM access_rules").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT singleton_id, revision FROM authorization_policy_state"
        ).fetchall() == [(1, 0)]


def test_valid_user_and_group_rules_cover_every_scope_and_effect(
    acl_database: Path,
) -> None:
    with _connect(acl_database) as connection:
        connection.execute(
            """
            INSERT INTO group_memberships(
                id, group_id, user_id, created_by, created_at
            )
            VALUES (1, 1, 2, 1, ?)
            """,
            (NOW,),
        )
        rules = (
            _rule(),
            _rule(
                id=2,
                principal_type="GROUP",
                user_id=None,
                group_id=1,
                scope_type="CABINET",
                scope_id=10,
                effect="DENY",
                expires_at=LATER,
            ),
            _rule(id=3, scope_type="FOLDER", scope_id=20, inherits=0),
            _rule(
                id=4,
                principal_type="GROUP",
                user_id=None,
                group_id=1,
                scope_type="DOC",
                scope_id=30,
                inherits=0,
            ),
        )
        connection.executemany(ACCESS_RULE_INSERT, rules)

        assert connection.execute(
            """
            SELECT principal_type, scope_type, effect
            FROM access_rules
            ORDER BY id
            """
        ).fetchall() == [
            ("USER", "GLOBAL", "ALLOW"),
            ("GROUP", "CABINET", "DENY"),
            ("USER", "FOLDER", "ALLOW"),
            ("GROUP", "DOC", "ALLOW"),
        ]


@pytest.mark.parametrize(
    "overrides",
    (
        {"principal_type": "user"},
        {"principal_type": "ROLE"},
        {"user_id": None},
        {"group_id": 1},
        {
            "principal_type": "GROUP",
            "user_id": 2,
            "group_id": 1,
        },
        {
            "principal_type": "GROUP",
            "user_id": None,
            "group_id": None,
        },
        {"scope_type": "global"},
        {"scope_type": "FILE"},
        {"scope_id": 1},
        {"scope_type": "CABINET", "scope_id": None},
        {"scope_type": "FOLDER", "scope_id": 0},
        {"scope_type": "DOC", "scope_id": -1},
        {"effect": "allow"},
        {"effect": "BLOCK"},
        {"inherits": 2},
        {"is_active": -1},
        {"reason": ""},
        {"reason": " not-trimmed"},
    ),
)
def test_rule_checks_reject_invalid_principal_scope_effect_and_metadata(
    acl_database: Path,
    overrides: dict[str, object],
) -> None:
    _assert_integrity_error(
        acl_database,
        ACCESS_RULE_INSERT,
        _rule(**overrides),
    )


@pytest.mark.parametrize(
    ("statement", "parameters"),
    (
        (
            """
            INSERT INTO groups(
                id, name, description, is_active, created_by, created_at, updated_at
            )
            VALUES (2, 'Orphan creator', '', 1, 999, ?, ?)
            """,
            (NOW, NOW),
        ),
        (
            """
            INSERT INTO group_memberships(
                id, group_id, user_id, created_by, created_at
            )
            VALUES (1, 999, 2, 1, ?)
            """,
            (NOW,),
        ),
        (
            """
            INSERT INTO group_memberships(
                id, group_id, user_id, created_by, created_at
            )
            VALUES (1, 1, 999, 1, ?)
            """,
            (NOW,),
        ),
        (
            """
            INSERT INTO group_memberships(
                id, group_id, user_id, created_by, created_at
            )
            VALUES (1, 1, 2, 999, ?)
            """,
            (NOW,),
        ),
        (
            ACCESS_RULE_INSERT,
            _rule(user_id=999),
        ),
        (
            ACCESS_RULE_INSERT,
            _rule(
                principal_type="GROUP",
                user_id=None,
                group_id=999,
            ),
        ),
        (
            ACCESS_RULE_INSERT,
            _rule(permission_id=999),
        ),
        (
            ACCESS_RULE_INSERT,
            _rule(created_by=999),
        ),
        (
            """
            UPDATE authorization_policy_state
            SET updated_by = 999
            WHERE singleton_id = 1
            """,
            (),
        ),
    ),
)
def test_foreign_keys_prevent_orphan_principals_permissions_and_creators(
    acl_database: Path,
    statement: str,
    parameters: tuple[object, ...] | dict[str, object],
) -> None:
    _assert_integrity_error(acl_database, statement, parameters)


def test_duplicate_memberships_and_equivalent_active_rules_are_rejected(
    acl_database: Path,
) -> None:
    with _connect(acl_database) as connection:
        connection.execute(
            """
            INSERT INTO group_memberships(
                id, group_id, user_id, created_by, created_at
            )
            VALUES (1, 1, 2, 1, ?)
            """,
            (NOW,),
        )

    _assert_integrity_error(
        acl_database,
        """
        INSERT INTO group_memberships(
            id, group_id, user_id, created_by, created_at
        )
        VALUES (2, 1, 2, 1, ?)
        """,
        (NOW,),
    )

    with _connect(acl_database) as connection:
        connection.execute(
            ACCESS_RULE_INSERT,
            _rule(id=1, is_active=0),
        )
        connection.execute(
            ACCESS_RULE_INSERT,
            _rule(id=2, is_active=0),
        )
        connection.execute(
            ACCESS_RULE_INSERT,
            _rule(id=3, expires_at=LATER),
        )
        connection.execute(
            ACCESS_RULE_INSERT,
            _rule(id=4, expires_at="2026-08-02 12:00:00"),
        )
        assert connection.execute("SELECT COUNT(*) FROM access_rules").fetchone() == (
            4,
        )


@pytest.mark.parametrize(
    "rule_overrides",
    (
        {},
        {"expires_at": LATER},
        {"scope_type": "FOLDER", "scope_id": 20},
        {
            "scope_type": "FOLDER",
            "scope_id": 20,
            "expires_at": LATER,
        },
        {
            "principal_type": "GROUP",
            "user_id": None,
            "group_id": 1,
        },
        {
            "principal_type": "GROUP",
            "user_id": None,
            "group_id": 1,
            "expires_at": LATER,
        },
        {
            "principal_type": "GROUP",
            "user_id": None,
            "group_id": 1,
            "scope_type": "DOC",
            "scope_id": 30,
        },
        {
            "principal_type": "GROUP",
            "user_id": None,
            "group_id": 1,
            "scope_type": "DOC",
            "scope_id": 30,
            "expires_at": LATER,
        },
    ),
)
def test_each_active_rule_shape_rejects_an_equivalent_duplicate(
    acl_database: Path,
    rule_overrides: dict[str, object],
) -> None:
    with _connect(acl_database) as connection:
        connection.execute(
            ACCESS_RULE_INSERT,
            _rule(**rule_overrides),
        )

    _assert_integrity_error(
        acl_database,
        ACCESS_RULE_INSERT,
        _rule(id=2, **rule_overrides),
    )


def test_referenced_acl_parents_cannot_be_deleted(
    acl_database: Path,
) -> None:
    with _connect(acl_database) as connection:
        connection.execute(
            """
            INSERT INTO group_memberships(
                id, group_id, user_id, created_by, created_at
            )
            VALUES (1, 1, 2, 1, ?)
            """,
            (NOW,),
        )
        connection.execute(ACCESS_RULE_INSERT, _rule())

    for statement in (
        "DELETE FROM users WHERE id = 1",
        "DELETE FROM users WHERE id = 2",
        "DELETE FROM groups WHERE id = 1",
        "DELETE FROM permissions WHERE id = 100",
    ):
        _assert_integrity_error(acl_database, statement, ())


@pytest.mark.parametrize(
    ("statement", "parameters"),
    (
        (
            """
            INSERT INTO authorization_policy_state(
                singleton_id, revision, updated_at, updated_by
            )
            VALUES (2, 1, ?, NULL)
            """,
            (NOW,),
        ),
        (
            """
            UPDATE authorization_policy_state
            SET revision = -1
            WHERE singleton_id = 1
            """,
            (),
        ),
    ),
)
def test_policy_state_is_singleton_and_revision_is_nonnegative(
    acl_database: Path,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    _assert_integrity_error(acl_database, statement, parameters)


def test_downgrade_and_forward_recreate_only_empty_acl_state(
    acl_database: Path,
) -> None:
    with _connect(acl_database) as connection:
        connection.execute(
            """
            INSERT INTO group_memberships(
                id, group_id, user_id, created_by, created_at
            )
            VALUES (1, 1, 2, 1, ?)
            """,
            (NOW,),
        )
        connection.execute(ACCESS_RULE_INSERT, _rule())
        connection.execute(
            """
            UPDATE authorization_policy_state
            SET revision = 7, updated_at = ?, updated_by = 1
            WHERE singleton_id = 1
            """,
            (LATER,),
        )

    legacy_before = _table_rows(acl_database)
    config = _config(acl_database)
    command.downgrade(config, PREVIOUS_REVISION)

    assert _current_revision(acl_database) == PREVIOUS_REVISION
    assert _table_rows(acl_database) == legacy_before
    engine = create_engine(f"sqlite:///{acl_database}")
    try:
        assert not set(ACL_TABLES).intersection(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, HEAD_REVISION)

    assert _current_revision(acl_database) == HEAD_REVISION
    assert _table_rows(acl_database) == legacy_before
    with sqlite3.connect(acl_database) as connection:
        assert connection.execute("SELECT * FROM groups").fetchall() == []
        assert connection.execute("SELECT * FROM group_memberships").fetchall() == []
        assert connection.execute("SELECT * FROM access_rules").fetchall() == []
        assert connection.execute(
            "SELECT singleton_id, revision, updated_by FROM authorization_policy_state"
        ).fetchall() == [(1, 0, None)]
