"""Filesystem persistence for Open Knowledge Format bundles."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..utils.okf_helpers import OkfEntry, parse_entry

BundleRevision = tuple[tuple[str, int, int, int, int], ...]


def bundle_revision(bundle_dir: Path) -> BundleRevision:
    """Return a process-independent fingerprint of the current Markdown set."""

    if not bundle_dir.exists() or not bundle_dir.is_dir():
        return ()
    resolved_root = bundle_dir.resolve()
    revision: list[tuple[str, int, int, int, int]] = []
    for markdown_path in sorted(bundle_dir.rglob("*.md")):
        try:
            resolved_path = markdown_path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_root):
                continue
            stat = resolved_path.stat()
            relative_path = resolved_path.relative_to(resolved_root).as_posix()
        except OSError:
            continue
        revision.append(
            (
                relative_path,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
        )
    return tuple(revision)


def load_bundle(bundle_dir: Path) -> list[OkfEntry]:
    if not bundle_dir.exists() or not bundle_dir.is_dir():
        return []
    resolved_root = bundle_dir.resolve()
    entries: list[OkfEntry] = []
    for markdown_path in sorted(bundle_dir.rglob("*.md")):
        try:
            resolved_path = markdown_path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_root):
                continue
            raw = resolved_path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        entries.append(parse_entry(markdown_path, raw))
    return entries


def save_entry(bundle_dir: Path, filename: str, content: str) -> Path:
    """Atomically write one contained entry.

    Readers in other worker processes observe either the complete previous file
    or the complete replacement, never a partially written Markdown document.
    """

    bundle_dir.mkdir(parents=True, exist_ok=True)
    relative = Path(filename)
    if relative.is_absolute():
        raise ValueError("OKF filename must be relative to the bundle directory")

    destination = bundle_dir / relative
    resolved_root = bundle_dir.resolve()
    resolved_destination = destination.resolve()
    if not resolved_destination.is_relative_to(resolved_root):
        raise ValueError("OKF filename must stay inside the bundle directory")

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return destination
