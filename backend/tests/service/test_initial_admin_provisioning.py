"""One-time administrator provisioning and password-source coverage."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _strong_password() -> str:
    return f"Qz9!{uuid4().hex}{uuid4().hex}"


def _candidate(password: str):
    from app.services.provisioning_service import InitialAdministrator

    return InitialAdministrator(
        username="initial.admin",
        name="Initial Administrator",
        email="initial.admin@example.com",
        password=password,
    )


def test_initial_administrator_is_atomic_audited_and_one_time(
    db_session: Session,
) -> None:
    from app import models
    from app.services import provisioning_service, rbac_service
    from app.utils.security import verify_password

    password = _strong_password()
    result = provisioning_service.provision_initial_administrator(
        db_session,
        _candidate(password),
    )

    user = db_session.query(models.User).one()
    assert user.id == result.user_id
    assert user.username == "initial.admin"
    assert user.status == "active"
    assert verify_password(password, user.password_hash)
    assignment = db_session.query(models.Assignment).one()
    assert assignment.user_id == user.id
    assert assignment.scope_type == "GLOBAL"
    assert assignment.scope_id is None
    assert assignment.effect == "ALLOW"
    assert assignment.role.name == "Super Admin"
    assert {row.permission.code for row in assignment.role.permissions} == set(
        rbac_service.PERMISSIONS
    )
    assert db_session.query(models.Cabinet).count() == 1
    assert db_session.query(models.Folder).count() == 1

    audit = db_session.query(models.AuditLog).one()
    assert audit.action == "INITIAL_ADMIN_PROVISIONED"
    assert audit.actor_id == user.id
    assert password not in audit.details
    assert json.loads(audit.details) == {
        "method": "one_time_secure_provisioning",
        "outcome": "created",
    }

    db_session.rollback()
    with pytest.raises(
        provisioning_service.AlreadyProvisionedError,
        match="PROVISION_ALREADY_INITIALIZED",
    ):
        provisioning_service.provision_initial_administrator(
            db_session,
            provisioning_service.InitialAdministrator(
                username="second.admin",
                name="Second Administrator",
                email="second.admin@example.com",
                password=_strong_password(),
            ),
        )

    assert db_session.query(models.User).count() == 1
    assert db_session.query(models.AuditLog).count() == 1


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        (
            {
                "username": "Uppercase",
                "name": "Valid Name",
                "email": "valid@example.com",
                "password": "Aa9!long-enough-random-value",
            },
            "PROVISION_USERNAME_INVALID",
        ),
        (
            {
                "username": "valid.admin",
                "name": "Valid Name",
                "email": "not-an-email",
                "password": "Aa9!long-enough-random-value",
            },
            "PROVISION_EMAIL_INVALID",
        ),
        (
            {
                "username": "valid.admin",
                "name": "Valid Name",
                "email": "valid@example.com",
                "password": "short",
            },
            "PROVISION_PASSWORD_POLICY",
        ),
        (
            {
                "username": "valid.admin",
                "name": "Valid Name",
                "email": "valid@example.com",
                "password": "Valid.Admin-Aa9!unsafe",
            },
            "PROVISION_PASSWORD_POLICY",
        ),
    ],
)
def test_initial_administrator_input_fails_before_database_writes(
    candidate: dict[str, str],
    code: str,
    db_session: Session,
) -> None:
    from app import models
    from app.services import provisioning_service

    with pytest.raises(provisioning_service.ProvisioningError, match=code):
        provisioning_service.provision_initial_administrator(
            db_session,
            provisioning_service.InitialAdministrator(**candidate),
        )

    assert db_session.query(models.User).count() == 0
    assert db_session.query(models.Role).count() == 0
    assert db_session.query(models.AuditLog).count() == 0


def test_password_file_requires_absolute_regular_owner_only_no_follow(
    settings_env: dict[str, str],
    test_paths,
) -> None:
    from app import provision_admin

    password = _strong_password()
    password_file = test_paths.root / "initial-admin-password"
    password_file.write_text(f"{password}\n", encoding="utf-8")
    password_file.chmod(0o600)

    assert provision_admin.read_password_file(password_file) == password

    password_file.chmod(0o644)
    with pytest.raises(
        provision_admin.PasswordSourceError,
        match="PROVISION_PASSWORD_FILE_UNSAFE",
    ):
        provision_admin.read_password_file(password_file)

    password_file.chmod(0o600)
    symlink = test_paths.root / "password-link"
    symlink.symlink_to(password_file)
    with pytest.raises(
        provision_admin.PasswordSourceError,
        match="PROVISION_PASSWORD_FILE",
    ):
        provision_admin.read_password_file(symlink)

    with pytest.raises(
        provision_admin.PasswordSourceError,
        match="PROVISION_PASSWORD_FILE_ABSOLUTE",
    ):
        provision_admin.read_password_file(Path("relative-password"))

    symlinked_parent = test_paths.root / "linked-parent"
    actual_parent = test_paths.root / "actual-parent"
    actual_parent.mkdir()
    parent_secret = actual_parent / "secret"
    parent_secret.write_text(password, encoding="utf-8")
    parent_secret.chmod(0o600)
    symlinked_parent.symlink_to(actual_parent, target_is_directory=True)
    with pytest.raises(
        provision_admin.PasswordSourceError,
        match="PROVISION_PASSWORD_FILE_UNSAFE",
    ):
        provision_admin.read_password_file(symlinked_parent / "secret")


def test_cli_provisions_once_without_disclosing_secret_or_path(
    test_paths,
    migrated_test_database: Path,
) -> None:
    assert migrated_test_database == test_paths.database
    password = _strong_password()
    password_file = test_paths.root / "operator-secret"
    password_file.write_text(f"{password}\n", encoding="utf-8")
    password_file.chmod(0o600)
    environment = os.environ.copy()
    environment.update(
        {
            "DOCVAULT_ENVIRONMENT": "test",
            "DOCVAULT_DATABASE_URL": f"sqlite:///{test_paths.database}",
            "DOCVAULT_SECRET_KEY": f"test-{uuid4().hex}-{uuid4().hex}",
            "DOCVAULT_STORAGE_DIR": str(test_paths.storage),
            "DOCVAULT_OKF_BUNDLE_DIR": str(test_paths.okf_bundle),
            "DOCVAULT_ENABLE_DEMO_SEED": "false",
            "DOCVAULT_LLM_PROVIDER": "none",
            "DOCVAULT_USE_DOCLING": "false",
            "DOCVAULT_USE_QDRANT": "false",
            "DOCVAULT_EMBEDDING_MODEL": "",
            "DOCVAULT_RERANKER_MODEL": "",
        }
    )
    command = [
        sys.executable,
        "-m",
        "app.provision_admin",
        "--username",
        "first.operator",
        "--name",
        "First Operator",
        "--email",
        "first.operator@example.com",
        "--password-file",
        str(password_file),
    ]

    first = subprocess.run(
        command,
        cwd=BACKEND_DIR,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0
    assert first.stdout == "Initial administrator provisioned.\n"
    assert first.stderr == ""

    second = subprocess.run(
        command,
        cwd=BACKEND_DIR,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 73
    assert second.stdout == ""
    assert "PROVISION_ALREADY_INITIALIZED" in second.stderr
    for forbidden in (password, str(password_file), str(test_paths.database)):
        assert forbidden not in first.stdout + first.stderr
        assert forbidden not in second.stdout + second.stderr

    with sqlite3.connect(test_paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (1,)
        password_hash = connection.execute(
            "SELECT password_hash FROM users"
        ).fetchone()[0]
        audit_details = connection.execute("SELECT details FROM audit_log").fetchone()[
            0
        ]
    assert password not in password_hash
    assert password not in audit_details


def test_demo_seed_generates_only_new_local_credentials(
    db_session: Session,
) -> None:
    from app import models, seed
    from app.utils.security import verify_password

    passwords = {
        identity.username: _strong_password() for identity in seed.DEMO_IDENTITIES
    }
    created = seed.seed_demo_database(
        db_session,
        password_factory=lambda username: passwords[username],
    )

    assert {credential.username for credential in created} == set(passwords)
    for user in db_session.query(models.User).all():
        assert verify_password(passwords[user.username], user.password_hash)

    called = False

    def must_not_generate(_username: str) -> str:
        nonlocal called
        called = True
        return _strong_password()

    assert (
        seed.seed_demo_database(
            db_session,
            password_factory=must_not_generate,
        )
        == []
    )
    assert called is False
    assert db_session.query(models.User).count() == len(passwords)
