"""Operator CLI for one-time initial-administrator provisioning."""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from .database import SessionLocal
from .runtime import settings
from .schema_compatibility import SchemaCompatibilityError, assert_schema_compatible
from .services import provisioning_service

_MAX_PASSWORD_FILE_BYTES = 258


class PasswordSourceError(RuntimeError):
    """Password input failed without retaining a filesystem path or raw error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def read_password_file(path: Path) -> str:
    """Read an owner-only regular file without following a symlink."""

    if not path.is_absolute():
        raise PasswordSourceError("PROVISION_PASSWORD_FILE_ABSOLUTE")

    descriptor = -1
    try:
        if _has_symlink_component(path) or path.resolve(strict=True) != path:
            raise PasswordSourceError("PROVISION_PASSWORD_FILE_UNSAFE")
        before = path.stat(follow_symlinks=False)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_nlink != 1
            or not opened.st_mode & stat.S_IRUSR
            or opened.st_mode & 0o077
        ):
            raise PasswordSourceError("PROVISION_PASSWORD_FILE_UNSAFE")
        descriptor_path = Path(f"/proc/self/fd/{descriptor}")
        if descriptor_path.exists() and descriptor_path.resolve(strict=True) != path:
            raise PasswordSourceError("PROVISION_PASSWORD_FILE_UNSAFE")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(_MAX_PASSWORD_FILE_BYTES)
    except PasswordSourceError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PasswordSourceError("PROVISION_PASSWORD_FILE_UNAVAILABLE") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw) >= _MAX_PASSWORD_FILE_BYTES or b"\x00" in raw:
        raise PasswordSourceError("PROVISION_PASSWORD_FILE_INVALID")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PasswordSourceError("PROVISION_PASSWORD_FILE_INVALID") from exc
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if "\n" in value or "\r" in value:
        raise PasswordSourceError("PROVISION_PASSWORD_FILE_INVALID")
    return value


def _interactive_password() -> str:
    if not sys.stdin.isatty():
        raise PasswordSourceError("PROVISION_PASSWORD_SOURCE_REQUIRED")
    password = getpass.getpass("Initial administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise PasswordSourceError("PROVISION_PASSWORD_MISMATCH")
    return password


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision the first DocVault administrator exactly once.",
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--password-file",
        type=Path,
        help="Absolute owner-readable file; omit only for an interactive TTY prompt.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        assert_schema_compatible(settings.database_url)
        password = (
            read_password_file(args.password_file)
            if args.password_file is not None
            else _interactive_password()
        )
        candidate = provisioning_service.validate_initial_administrator(
            provisioning_service.InitialAdministrator(
                username=args.username,
                name=args.name,
                email=args.email,
                password=password,
            )
        )
        db = SessionLocal()
        try:
            provisioning_service.provision_initial_administrator(db, candidate)
        finally:
            db.close()
    except SchemaCompatibilityError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    except PasswordSourceError as exc:
        print(
            f"Initial administrator provisioning rejected ({exc.code}).",
            file=sys.stderr,
        )
        return 65
    except provisioning_service.AlreadyProvisionedError as exc:
        print(
            f"Initial administrator provisioning rejected ({exc.code}).",
            file=sys.stderr,
        )
        return 73
    except provisioning_service.ProvisioningError as exc:
        print(
            f"Initial administrator provisioning rejected ({exc.code}).",
            file=sys.stderr,
        )
        return 65
    except Exception:
        print(
            "Initial administrator provisioning failed (PROVISION_INTERNAL_ERROR).",
            file=sys.stderr,
        )
        return 70

    print("Initial administrator provisioned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
