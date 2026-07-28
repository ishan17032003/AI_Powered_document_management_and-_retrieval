"""One-time initial-administrator provisioning."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.orm import Session

from ..repositories import provisioning_repository
from ..utils.security import hash_password
from . import bootstrap_catalog_service

_USERNAME = re.compile(r"^[a-z][a-z0-9._-]{2,79}$")
_INITIAL_ADMIN_ROLE = "Super Admin"
_PROVISION_LOCK_KEY = 2_024_072_701
_MIN_PASSWORD_CHARS = 16
_MAX_PASSWORD_CHARS = 256


class ProvisioningError(RuntimeError):
    """Safe provisioning failure with a stable operator-facing code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AlreadyProvisionedError(ProvisioningError):
    pass


@dataclass(frozen=True)
class InitialAdministrator:
    username: str
    name: str
    email: str
    password: str


@dataclass(frozen=True)
class ProvisioningResult:
    user_id: int


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def validate_initial_administrator(
    candidate: InitialAdministrator,
) -> InitialAdministrator:
    """Normalize public identity fields and enforce a bootstrap password floor."""

    username = candidate.username.strip()
    name = unicodedata.normalize("NFKC", candidate.name).strip()
    if not _USERNAME.fullmatch(username):
        raise ProvisioningError("PROVISION_USERNAME_INVALID")
    if not name or len(name) > 160 or _contains_control(name):
        raise ProvisioningError("PROVISION_NAME_INVALID")
    try:
        email = validate_email(
            candidate.email.strip(),
            check_deliverability=False,
        ).normalized
    except EmailNotValidError as exc:
        raise ProvisioningError("PROVISION_EMAIL_INVALID") from exc
    if len(email) > 200:
        raise ProvisioningError("PROVISION_EMAIL_INVALID")

    password = candidate.password
    if (
        not _MIN_PASSWORD_CHARS <= len(password) <= _MAX_PASSWORD_CHARS
        or password != password.strip()
        or _contains_control(password)
    ):
        raise ProvisioningError("PROVISION_PASSWORD_POLICY")

    character_classes = sum(
        (
            any(character.islower() for character in password),
            any(character.isupper() for character in password),
            any(character.isdigit() for character in password),
            any(not character.isalnum() for character in password),
        )
    )
    email_local = email.partition("@")[0].casefold()
    password_folded = password.casefold()
    if (
        character_classes < 3
        or username.casefold() in password_folded
        or (len(email_local) >= 3 and email_local in password_folded)
        or "docvault" in password_folded
    ):
        raise ProvisioningError("PROVISION_PASSWORD_POLICY")

    return InitialAdministrator(
        username=username,
        name=name,
        email=email,
        password=password,
    )


def provision_initial_administrator(
    db: Session,
    candidate: InitialAdministrator,
) -> ProvisioningResult:
    """Atomically create the only allowed first account and its audit record."""

    validated = validate_initial_administrator(candidate)
    try:
        if not provisioning_repository.acquire_database_lock(
            db,
            _PROVISION_LOCK_KEY,
        ):
            raise ProvisioningError("PROVISION_DATABASE_UNSUPPORTED")
        if provisioning_repository.has_any_user(db):
            raise AlreadyProvisionedError("PROVISION_ALREADY_INITIALIZED")

        permissions = bootstrap_catalog_service.ensure_permissions(db)
        roles = bootstrap_catalog_service.ensure_roles(db, permissions)
        role = roles.get(_INITIAL_ADMIN_ROLE)
        if role is None:
            raise ProvisioningError("PROVISION_ROLE_UNAVAILABLE")

        user = provisioning_repository.add_user(
            db,
            username=validated.username,
            name=validated.name,
            email=validated.email,
            password_hash=hash_password(validated.password),
        )
        provisioning_repository.add_global_assignment(
            db,
            user_id=user.id,
            role_id=role.id,
        )
        bootstrap_catalog_service.ensure_hierarchy(db)
        provisioning_repository.add_provisioning_audit(
            db,
            user=user,
            details=json.dumps(
                {
                    "method": "one_time_secure_provisioning",
                    "outcome": "created",
                },
                sort_keys=True,
            ),
        )
        db.commit()
        return ProvisioningResult(user_id=user.id)
    except Exception:
        db.rollback()
        raise
