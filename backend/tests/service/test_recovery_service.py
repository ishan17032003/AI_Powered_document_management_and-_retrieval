import os
import sqlite3
import time
from pathlib import Path

from app import models
from app.services.recovery_service import (
    coordinated_backup,
    garbage_collection_candidates,
    rehearse_dr,
    restore_from_manifest,
)


def test_coordinated_backup_restore_and_dr_rehearsal(tmp_path: Path) -> None:
    database = tmp_path / "docvault.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE survivor (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO survivor VALUES (1)")
    storage = tmp_path / "storage"
    (storage / "objects" / "aa").mkdir(parents=True)
    (storage / "objects" / "aa" / "object").write_bytes(b"document")
    okf = tmp_path / "okf"
    okf.mkdir()
    (okf / "entry.md").write_text("# entry", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.toml").write_text("profile='test'\n", encoding="utf-8")
    backup = tmp_path / "backup"
    manifest = coordinated_backup(database, storage, okf, backup, config_root=config, release_version="test", schema_version="head", quiescent_confirmed=True)
    assert manifest["source_quiescent_confirmed"] is True
    assert manifest["config_included"] is True
    assert any(entry["path"] == "config/runtime.toml" for entry in manifest["files"])
    restored = restore_from_manifest(backup / "backup-manifest.json", tmp_path / "restore")
    assert restored["quick_check"] == ["ok"]
    assert restored["derived_indexes"]["checked"] is True
    assert restored["migration"]["applied"] is False
    rehearsal = rehearse_dr(backup / "backup-manifest.json", tmp_path / "dr")
    assert rehearsal["status"] == "passed"
    assert rehearsal["scenarios"]["object_loss"] is True
    assert rehearsal["timing"]["restore_and_rehearsal_ms"] >= 0


def test_gc_candidates_is_bounded_dry_run_and_preserves_referenced_objects(
    db_session, user_factory, tmp_path: Path
) -> None:
    user = user_factory()
    cabinet = models.Cabinet(name="GC cabinet")
    db_session.add(cabinet)
    db_session.flush()
    folder = models.Folder(cabinet_id=cabinet.id, name="GC folder")
    db_session.add(folder)
    db_session.flush()
    document = models.Document(
        folder_id=folder.id,
        title="Referenced",
        content_hash="a" * 64,
        created_by=user.id,
        lifecycle_state="ACTIVE",
    )
    db_session.add(document)
    db_session.flush()
    referenced = tmp_path / "objects" / "aa" / "referenced"
    referenced.parent.mkdir(parents=True)
    referenced.write_bytes(b"keep")
    db_session.add(
        models.DocVersion(
            document_id=document.id,
            version_no=1,
            file_key="objects/aa/referenced",
            filename="keep.txt",
            checksum="b" * 64,
            size=4,
            created_by=user.id,
            storage_state="AVAILABLE",
        )
    )
    db_session.flush()
    orphan = tmp_path / "objects" / "bb" / "orphan"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"remove later")
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    symlink = tmp_path / "objects" / "bb" / "link"
    symlink.symlink_to(orphan)
    report = garbage_collection_candidates(
        db_session, tmp_path, older_than_hours=1, max_items=10
    )
    assert report["dry_run"] is True
    assert report["bounded"] is False
    assert {item["path"] for item in report["candidates"]} == {"objects/bb/orphan"}
    assert referenced.exists() and orphan.exists() and symlink.is_symlink()


def test_gc_candidates_rejects_unbounded_inputs(db_session, tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        garbage_collection_candidates(db_session, tmp_path, older_than_hours=0)
    with pytest.raises(ValueError):
        garbage_collection_candidates(db_session, tmp_path, max_items=0)
