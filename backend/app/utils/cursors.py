"""Bounded opaque cursor encoding for deterministic keyset pagination."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime


def encode_cursor(**values: object) -> str:
    raw = json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> dict[str, object] | None:
    if value is None:
        return None
    if not 1 <= len(value) <= 256:
        raise ValueError("invalid cursor")
    try:
        padded = value.encode() + b"=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (binascii.Error, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid cursor")
    return decoded


def cursor_int(values: dict[str, object], key: str) -> int:
    value = values.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError("invalid cursor")
    return value


def cursor_time(values: dict[str, object], key: str) -> datetime:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError("invalid cursor")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid cursor")
    return parsed
