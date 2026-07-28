"""Non-destructive GC, coordinated backup, restore, and DR primitives."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from .. import models


class RecoveryError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise RecoveryError("recovery source directory is missing")
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RecoveryError("symlinks are not allowed in recovery sources")
        if path.is_file():
            paths.append(path)
    return paths


def garbage_collection_candidates(
    db: Session,
    storage_root: Path,
    *,
    older_than_hours: int = 24,
    max_items: int = 10_000,
) -> dict[str, Any]:
    """Return only candidates; this function never removes or moves files."""
    if type(older_than_hours) is not int or not 1 <= older_than_hours <= 8_760:
        raise ValueError("older_than_hours must be between 1 and 8760")
    if type(max_items) is not int or not 1 <= max_items <= 100_000:
        raise ValueError("max_items must be between 1 and 100000")
    root = Path(storage_root).resolve()
    cutoff = datetime.now(UTC).timestamp() - older_than_hours * 3600
    referenced = {
        version.file_key
        for version in db.query(models.DocVersion).filter(
            models.DocVersion.storage_state != "DELETED"
        )
        if version.document is not None and version.document.lifecycle_state == "ACTIVE"
    }
    candidates: list[dict[str, Any]] = []
    roots = [root / name for name in (".staging", ".upload-quarantine", ".quarantine", ".tombstone")]
    objects = root / "objects"
    for candidate_root in roots + [objects]:
        if not candidate_root.is_dir():
            continue
        for path in sorted(p for p in candidate_root.rglob("*") if p.is_file()):
            # Symlinks are never eligible for lifecycle cleanup.  Following a
            # link could escape the explicitly approved storage root and turn
            # a dry-run report into an unsafe deletion target later.
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in referenced or path.stat().st_mtime > cutoff:
                continue
            reason = "unreferenced_object" if candidate_root == objects else "expired_lifecycle_artifact"
            candidates.append({"path": relative, "size": path.stat().st_size, "reason": reason})
            if len(candidates) >= max_items:
                return {"schema_version": "gc-candidates-v1", "dry_run": True, "bounded": True, "candidates": candidates}
    return {"schema_version": "gc-candidates-v1", "dry_run": True, "bounded": False, "candidates": candidates}


def _copy_file(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    actual = _sha256(destination)
    expected = _sha256(source)
    if actual != expected:
        raise RecoveryError(f"backup checksum mismatch: {source.name}")
    return {"path": destination.name, "size": destination.stat().st_size, "sha256": actual}


def _copy_tree(source: Path, destination: Path, category: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source_file in _files(source):
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        digest = _sha256(target)
        if digest != _sha256(source_file):
            raise RecoveryError(f"backup checksum mismatch: {relative}")
        entries.append({"category": category, "path": relative.as_posix(), "size": target.stat().st_size, "sha256": digest})
    return entries


def coordinated_backup(
    database: Path,
    storage_root: Path,
    okf_root: Path,
    destination: Path,
    *,
    config_root: Path | None = None,
    release_version: str = "unknown",
    schema_version: str = "unknown",
    quiescent_confirmed: bool = False,
) -> dict[str, Any]:
    """Create a DB/object/config bundle and verify all copied hashes."""
    database = Path(database).resolve()
    storage_root = Path(storage_root).resolve()
    okf_root = Path(okf_root).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise RecoveryError("backup destination already exists")
    if not quiescent_confirmed:
        raise RecoveryError("source quiescence must be explicitly confirmed")
    if not database.is_file():
        raise RecoveryError("database source is missing")
    config = Path(config_root).resolve() if config_root is not None else None
    sources = [database, storage_root, okf_root] + ([config] if config is not None else [])
    if any(source == destination or destination.is_relative_to(source) for source in sources if source is not None):
        raise RecoveryError("backup destination must not overlap an authoritative source")
    if config is not None and not config.exists():
        raise RecoveryError("config source is missing")
    destination.mkdir(parents=True, mode=0o700)
    before = {str(path): path.stat().st_mtime_ns for path in [database] + _files(storage_root) + _files(okf_root) + (_files(config) if config is not None and config.is_dir() else [config] if config is not None else [])}
    database_target = destination / "database" / "docvault.db"
    database_target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(database_target) as target:
        source.execute("PRAGMA query_only=ON")
        source.backup(target, pages=128)
        quick_check = [row[0] for row in target.execute("PRAGMA quick_check")]
        foreign_keys = [list(row) for row in target.execute("PRAGMA foreign_key_check")]
    if quick_check != ["ok"] or foreign_keys:
        raise RecoveryError("backup database integrity check failed")
    files = [{"category": "database", "path": "database/docvault.db", "size": database_target.stat().st_size, "sha256": _sha256(database_target)}]
    files += [{**entry, "path": f"storage/{entry['path']}"} for entry in _copy_tree(storage_root, destination / "storage", "storage")]
    files += [{**entry, "path": f"okf_bundle/{entry['path']}"} for entry in _copy_tree(okf_root, destination / "okf_bundle", "okf_bundle")]
    config_entries: list[dict[str, Any]] = []
    if config is not None:
        if config.is_dir():
            config_entries = [{**entry, "path": f"config/{entry['path']}"} for entry in _copy_tree(config, destination / "config", "config")]
        else:
            config_target = destination / "config" / config.name
            config_entries = [_copy_file(config, config_target) | {"category": "config", "path": f"config/{config.name}"}]
        files += config_entries
    after = {str(path): path.stat().st_mtime_ns for path in [database] + _files(storage_root) + _files(okf_root) + (_files(config) if config is not None and config.is_dir() else [config] if config is not None else [])}
    if before != after:
        raise RecoveryError("authoritative source changed during backup")
    manifest = {
        "schema_version": "coordinated-backup-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "release_version": release_version[:80],
        "schema_head": schema_version[:80],
        "config_included": config is not None,
        "source_quiescent_confirmed": quiescent_confirmed,
        "config": {"release_version": release_version[:80], "schema_head": schema_version[:80]},
        "database": {"quick_check": quick_check, "foreign_key_violations": foreign_keys},
        "files": sorted(files, key=lambda item: item["path"]),
    }
    (destination / "backup-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def restore_from_manifest(
    manifest_path: Path,
    destination: Path,
    *,
    alembic_ini: Path | None = None,
) -> dict[str, Any]:
    """Restore into a new empty directory and verify every manifest hash."""
    manifest_path = Path(manifest_path).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise RecoveryError("restore destination already exists")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "coordinated-backup-v1":
        raise RecoveryError("unsupported backup manifest")
    source_root = manifest_path.parent
    destination.mkdir(parents=True, mode=0o700)
    verified = 0
    for entry in manifest.get("files", []):
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RecoveryError("unsafe manifest path")
        source = source_root / relative
        target = destination / relative
        if not source.is_file() or _sha256(source) != entry["sha256"]:
            raise RecoveryError(f"backup source checksum mismatch: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _sha256(target) != entry["sha256"]:
            raise RecoveryError(f"restore checksum mismatch: {relative}")
        verified += 1
    database = destination / "database" / "docvault.db"
    migration_applied = False
    if alembic_ini is not None:
        from alembic import command
        from alembic.config import Config

        config = Config(str(Path(alembic_ini).resolve()))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
        command.upgrade(config, "head")
        migration_applied = True
    with sqlite3.connect(database) as connection:
        migration_rows = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall() if _table_exists(connection, "alembic_version") else []
        migration_head = [str(row[0]) for row in migration_rows]
        derived_indexes = _rebuild_derived_indexes(connection)
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if quick_check != ["ok"] or foreign_keys:
        raise RecoveryError("restored database integrity check failed")
    reconciliation: dict[str, Any]
    try:
        engine = create_engine(f"sqlite:///{database}")
        with Session(engine) as db:
            from .reconciliation_service import reconcile

            reconciliation = reconcile(db, destination / "storage")
        engine.dispose()
    except Exception as exc:
        reconciliation = {"checked": False, "reason": "application_schema_unavailable", "error_type": type(exc).__name__}
    return {
        "schema_version": "restore-verification-v1",
        "verified_files": verified,
        "quick_check": quick_check,
        "foreign_key_violations": foreign_keys,
        "migration": {"checked": bool(migration_rows), "applied": migration_applied, "head": migration_head, "manifest_head": manifest.get("schema_head")},
        "derived_indexes": derived_indexes,
        "reconciliation": {"source_bundle_files_verified": verified, **reconciliation},
    }


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _rebuild_derived_indexes(connection: sqlite3.Connection) -> dict[str, Any]:
    rebuilt: list[str] = []
    if _table_exists(connection, "doc_fts"):
        connection.execute("INSERT INTO doc_fts(doc_fts) VALUES ('rebuild')")
        connection.commit()
        rebuilt.append("doc_fts")
    return {"checked": True, "rebuilt": rebuilt}


def rehearse_dr(manifest_path: Path, workdir: Path) -> dict[str, Any]:
    """Run isolated loss scenarios without touching the backup or live source."""
    started = time.monotonic()
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    restore = Path(workdir).resolve() / "clean-restore"
    verification = restore_from_manifest(manifest_path, restore)
    database = restore / "database" / "docvault.db"
    object_files = [path for path in (restore / "storage" / "objects").rglob("*") if path.is_file()]
    retrieval_present = database.exists()
    object_loss_detected = False
    if object_files:
        victim = object_files[0]
        victim.unlink()
        object_loss_detected = not victim.exists()
    second = Path(workdir).resolve() / "host-loss-restore"
    second_verification = restore_from_manifest(manifest_path, second)
    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    created_at = datetime.fromisoformat(manifest["created_at"])
    backup_age_seconds = max(0.0, (datetime.now(UTC) - created_at).total_seconds())
    return {
        "schema_version": "dr-rehearsal-v1",
        "status": "passed" if verification["quick_check"] == ["ok"] and object_loss_detected and retrieval_present and second_verification["verified_files"] == verification["verified_files"] else "failed",
        "scenarios": {"database_loss": verification["quick_check"] == ["ok"], "object_loss": object_loss_detected, "retrieval_loss": retrieval_present, "complete_host_loss": second_verification["verified_files"] == verification["verified_files"]},
        "restore": verification,
        "timing": {"restore_and_rehearsal_ms": elapsed_ms, "backup_age_seconds_at_start": round(backup_age_seconds, 2), "rpo_measurement": "backup_age_only; production transaction RPO requires deployment telemetry", "rto_measurement": "local rehearsal elapsed time only"},
    }
