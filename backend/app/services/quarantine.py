"""Bounded streaming upload quarantine primitive.

This module deliberately has no database or object-store side effects beyond
creating a staged file. Promotion and lifecycle ownership remain STORE-001.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class QuarantinedUpload:
    path: Path
    size: int
    checksum: str


def stage_stream(stream: BinaryIO, *, directory: Path, max_bytes: int) -> QuarantinedUpload:
    """Copy a request stream to quarantine with bounded memory and hashing."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    handle = tempfile.NamedTemporaryFile(prefix="upload-", suffix=".part", dir=directory, delete=False)
    path = Path(handle.name)
    try:
        with handle:
            while chunk := stream.read(min(1024 * 1024, max_bytes - size + 1)):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("upload exceeds configured byte limit")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        return QuarantinedUpload(path=path, size=size, checksum=digest.hexdigest())
    except Exception:
        path.unlink(missing_ok=True)
        raise


__all__ = ["QuarantinedUpload", "stage_stream"]
