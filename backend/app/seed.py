"""Explicit development/test demo catalog and identity seed."""

from __future__ import annotations

import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import models
from .database import SessionLocal
from .runtime import settings
from .schema_compatibility import SchemaCompatibilityError, assert_schema_compatible
from .services import bootstrap_catalog_service
from .utils.security import hash_password


@dataclass(frozen=True)
class DemoIdentity:
    username: str
    name: str
    email: str
    role_name: str


@dataclass(frozen=True)
class CreatedDemoCredential:
    username: str
    password: str
    role_name: str


DEMO_IDENTITIES = (
    DemoIdentity(
        "admin",
        "Platform Admin",
        "admin@docvault.local",
        "Super Admin",
    ),
    DemoIdentity(
        "contributor",
        "Content Contributor",
        "contrib@docvault.local",
        "Contributor",
    ),
    DemoIdentity(
        "viewer",
        "Read-Only Viewer",
        "viewer@docvault.local",
        "Viewer",
    ),
    DemoIdentity(
        "auditor",
        "Compliance Auditor",
        "auditor@docvault.local",
        "Auditor",
    ),
)

PasswordFactory = Callable[[str], str]

FIXED_DEMO_PASSWORDS = {
    "admin": "admin123",
    "contributor": "contrib123",
    "viewer": "viewer123",
    "auditor": "auditor123",
}


def _random_demo_password(_username: str) -> str:
    return f"Dv1!{secrets.token_urlsafe(24)}"


def _configured_demo_password(username: str) -> str:
    if settings.demo_fixed_credentials:
        return FIXED_DEMO_PASSWORDS[username]
    return _random_demo_password(username)


def seed_demo_database(
    db: Session,
    *,
    password_factory: PasswordFactory | None = None,
) -> list[CreatedDemoCredential]:
    """Seed local demo data atomically and return only newly created credentials."""

    created: list[CreatedDemoCredential] = []
    try:
        permissions = bootstrap_catalog_service.ensure_permissions(db)
        roles = bootstrap_catalog_service.ensure_roles(db, permissions)
        
        # Users must exist before groups: groups.created_by is a NOT NULL FK
        # to users.id, which PostgreSQL enforces (SQLite silently did not).
        users_by_name: dict[str, models.User] = {}
        for identity in DEMO_IDENTITIES:
            user = (
                db.query(models.User)
                .filter(models.User.username == identity.username)
                .first()
            )
            if user is None:
                password = (password_factory or _configured_demo_password)(identity.username)
                user = models.User(
                    username=identity.username,
                    name=identity.name,
                    email=identity.email,
                    password_hash=hash_password(password),
                    status="active",
                )
                db.add(user)
                db.flush()
                created.append(
                    CreatedDemoCredential(
                        username=identity.username,
                        password=password,
                        role_name=identity.role_name,
                    )
                )
            elif settings.demo_fixed_credentials and password_factory is None:
                user.password_hash = hash_password(FIXED_DEMO_PASSWORDS[identity.username])
            users_by_name[identity.username] = user

        owner = users_by_name.get("admin") or next(iter(users_by_name.values()))
        for role_name in roles:
            group = db.query(models.Group).filter(models.Group.name == role_name).first()
            if not group:
                db.add(
                    models.Group(
                        name=role_name,
                        description=f"Group for {role_name} role",
                        is_active=True,
                        created_by=owner.id,
                    )
                )
        db.flush()

        for identity in DEMO_IDENTITIES:
            user = users_by_name[identity.username]
            role = roles.get(identity.role_name)
            if role is not None:
                assignment = (
                    db.query(models.Assignment)
                    .filter(
                        models.Assignment.user_id == user.id,
                        models.Assignment.role_id == role.id,
                    )
                    .first()
                )
                if assignment is None:
                    db.add(
                        models.Assignment(
                            user_id=user.id,
                            role_id=role.id,
                            scope_type="GLOBAL",
                            effect="ALLOW",
                        )
                    )
                
                # Sync group membership
                group = db.query(models.Group).filter(models.Group.name == role.name).first()
                if group:
                    membership = db.query(models.GroupMembership).filter(
                        models.GroupMembership.group_id == group.id,
                        models.GroupMembership.user_id == user.id
                    ).first()
                    if not membership:
                        db.add(
                            models.GroupMembership(
                                group_id=group.id,
                                user_id=user.id,
                                created_by=1
                            )
                        )
                
                # Demo identities are deliberately provisioned with explicit
                # global ACL rows as well as their capability bundle.  This is
                # development/test data only; production bootstrap never calls
                # the demo seed, and role membership alone never grants file
                # visibility under the resource-authorization policy.
                db.flush()
                permission_ids = {
                    role_permission.permission_id
                    for role_permission in role.permissions
                }
                existing_acl = {
                    rule.permission_id
                    for rule in db.query(models.AccessRule).filter(
                        models.AccessRule.principal_type == "USER",
                        models.AccessRule.user_id == user.id,
                        models.AccessRule.scope_type == "GLOBAL",
                        models.AccessRule.scope_id.is_(None),
                        models.AccessRule.effect == "ALLOW",
                        models.AccessRule.inherits.is_(True),
                        models.AccessRule.is_active.is_(True),
                        models.AccessRule.expires_at.is_(None),
                    )
                }
                for permission_id in sorted(permission_ids - existing_acl):
                    db.add(
                        models.AccessRule(
                            principal_type="USER",
                            user_id=user.id,
                            group_id=None,
                            permission_id=permission_id,
                            scope_type="GLOBAL",
                            scope_id=None,
                            effect="ALLOW",
                            inherits=True,
                            is_active=True,
                            expires_at=None,
                            reason="development demo global access",
                            created_by=user.id,
                        )
                    )
        bootstrap_catalog_service.ensure_hierarchy(db)
        db.commit()
        return created
    except Exception:
        db.rollback()
        raise


def main() -> int:
    if not settings.enable_demo_seed:
        print(
            "Demo seed disabled; no demo identities or sample hierarchy were created."
        )
        return 0
    if settings.is_production:
        print("Demo seed rejected outside development/test.")
        return 78

    try:
        assert_schema_compatible(settings.database_url)
    except SchemaCompatibilityError as exc:
        print(str(exc), file=sys.stderr)
        return 78

    db = SessionLocal()
    try:
        created = seed_demo_database(db)
    except Exception:
        print("Demo seed failed (SEED_INTERNAL_ERROR).", file=sys.stderr)
        return 70
    finally:
        db.close()

    print("Development demo seed complete.")
    if created:
        print("Generated one-time local credentials (save them now):")
        for credential in created:
            print(
                f"  {credential.username:<12} / "
                f"{credential.password} ({credential.role_name})"
            )
    else:
        print("No new demo identities were created; credentials were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
