"""Server-folder import containment, limits, and safe-error coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session


def _user_and_folder(db_session: Session, user_factory):
    from app import models

    user = user_factory(
        username="folder-import-policy-user",
        email="folder-import-policy-user@example.test",
    )
    cabinet = models.Cabinet(name="Import Policy Cabinet")
    db_session.add(cabinet)
    db_session.flush()
    folder = models.Folder(cabinet_id=cabinet.id, name="Import Policy Folder")
    db_session.add(folder)
    db_session.flush()
    return user, folder


def _enable(
    monkeypatch: pytest.MonkeyPatch,
    document_service,
    root: Path,
    **limits: int,
) -> None:
    monkeypatch.setattr(document_service.settings, "folder_import_enabled", True)
    monkeypatch.setattr(document_service.settings, "folder_import_roots", [root])
    for name, value in limits.items():
        monkeypatch.setattr(document_service.settings, name, value)


def _fake_upload(document_id: int = 101):
    from app import schemas

    return schemas.UploadResult(
        id=document_id,
        title="safe.txt",
        status="READY",
        folder_id=1,
        ocr_status="native",
        doc_class="Other",
        duplicate_of=None,
        pipeline={},
    )


def test_folder_import_is_disabled_by_default_with_safe_error(
    db_session: Session,
    user_factory,
    test_paths,
) -> None:
    from app import schemas
    from app.services import document_service
    from app.services.exceptions import PermissionDeniedError

    user, folder = _user_and_folder(db_session, user_factory)
    raw_path = str(test_paths.root / "private-path-canary")

    with pytest.raises(PermissionDeniedError) as raised:
        document_service.import_folder(
            db_session,
            user,
            schemas.ImportRequest(path=raw_path, folder_id=folder.id),
        )

    assert raised.value.detail == "Server folder import is disabled"
    assert raw_path not in raised.value.detail


@pytest.mark.parametrize(
    "requested",
    [
        "relative/import",
        "/approved/import/../import",
        "/outside/approved/root",
    ],
)
def test_relative_parent_and_outside_paths_are_rejected_without_echo(
    requested: str,
    db_session: Session,
    user_factory,
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import schemas
    from app.services import document_service
    from app.services.exceptions import PermissionDeniedError

    approved = test_paths.root / "approved"
    approved.mkdir()
    _enable(monkeypatch, document_service, approved)
    user, folder = _user_and_folder(db_session, user_factory)

    with pytest.raises(PermissionDeniedError) as raised:
        document_service.import_folder(
            db_session,
            user,
            schemas.ImportRequest(path=requested, folder_id=folder.id),
        )

    assert raised.value.detail == "Import location is not allowed"
    assert requested not in raised.value.detail


def test_allowed_file_import_uses_safe_source_identifier_in_response_and_audit(
    db_session: Session,
    user_factory,
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models, schemas
    from app.services import document_service

    approved = test_paths.root / "approved"
    approved.mkdir()
    (approved / "safe.txt").write_text(
        "Folder import policy test content.",
        encoding="utf-8",
    )
    _enable(monkeypatch, document_service, approved)
    user, folder = _user_and_folder(db_session, user_factory)

    result = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(path=str(approved), folder_id=folder.id),
    )

    assert result.imported == 1
    assert result.path.startswith("import-root-1-")
    rendered = result.model_dump_json()
    assert str(approved) not in rendered
    audit = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "IMPORT_FOLDER")
        .one()
    )
    assert audit.object_id == result.path
    assert str(approved) not in audit.object_id
    assert str(approved) not in audit.details


def test_symlink_and_changed_file_are_rejected_before_read(
    db_session: Session,
    user_factory,
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import schemas
    from app.services import document_service

    approved = test_paths.root / "approved"
    approved.mkdir()
    outside = test_paths.root / "outside-secret.txt"
    outside.write_text("outside-content-canary", encoding="utf-8")
    (approved / "linked.txt").symlink_to(outside)
    _enable(monkeypatch, document_service, approved)
    user, folder = _user_and_folder(db_session, user_factory)

    result = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(path=str(approved), folder_id=folder.id),
    )

    assert result.imported == 0
    assert result.skipped == 1
    assert result.items[0].detail == "symlink_rejected"
    rendered = result.model_dump_json()
    assert str(outside) not in rendered
    assert "outside-content-canary" not in rendered

    regular = approved / "regular.txt"
    regular.write_text("initial", encoding="utf-8")
    source = document_service._resolve_import_source(str(approved))
    enumerated = regular.stat(follow_symlinks=False)
    regular.unlink()
    regular.symlink_to(outside)

    with pytest.raises(document_service._UnsafeImportPath):
        document_service._read_import_file(source, regular, enumerated)


def test_mount_boundary_is_rejected(
    settings_env: dict[str, str],
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import document_service

    approved = test_paths.root / "approved"
    mounted = approved / "mounted"
    mounted.mkdir(parents=True)
    _enable(monkeypatch, document_service, approved)
    source = document_service._resolve_import_source(str(approved))
    original_is_mount = document_service.os.path.ismount

    monkeypatch.setattr(
        document_service.os.path,
        "ismount",
        lambda value: Path(value) == mounted.resolve() or original_is_mount(value),
    )

    with pytest.raises(document_service._UnsafeImportPath):
        document_service._validated_import_entry(
            source,
            mounted,
            require_directory=True,
        )

    nested_file = mounted / "nested.txt"
    nested_file.write_text("must remain unread", encoding="utf-8")
    with pytest.raises(document_service._UnsafeImportPath):
        document_service._validated_import_entry(
            source,
            nested_file,
            require_directory=False,
        )


def test_enumeration_file_depth_and_byte_limits_are_bounded(
    db_session: Session,
    user_factory,
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import schemas
    from app.services import document_service

    approved = test_paths.root / "approved"
    nested = approved / "nested"
    nested.mkdir(parents=True)
    (approved / "one.txt").write_text("one", encoding="utf-8")
    (approved / "two.txt").write_text("two", encoding="utf-8")
    (nested / "deep.txt").write_text("deep", encoding="utf-8")
    _enable(
        monkeypatch,
        document_service,
        approved,
        folder_import_max_files=10,
        folder_import_max_depth=0,
    )
    monkeypatch.setattr(
        document_service,
        "ingest_document",
        lambda *_args, **_kwargs: _fake_upload(),
    )
    monkeypatch.setattr(
        document_service.audit_service, "record", lambda *_a, **_k: None
    )
    user, folder = _user_and_folder(db_session, user_factory)

    result = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(path=str(approved), folder_id=folder.id),
    )

    assert result.imported == 2
    assert result.skipped >= 1
    details = {item.detail for item in result.items}
    assert "depth_limit" in details

    monkeypatch.setattr(document_service.settings, "folder_import_max_files", 1)
    file_limited = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(
            path=str(approved),
            folder_id=folder.id,
            recursive=False,
        ),
    )
    assert file_limited.imported == 1
    assert "accepted_file_limit" in {item.detail for item in file_limited.items}

    monkeypatch.setattr(document_service.settings, "folder_import_max_files", 10)
    monkeypatch.setattr(document_service.settings, "folder_import_max_total_mb", 1)
    (approved / "large-a.bin").write_bytes(b"a" * 700_000)
    (approved / "large-b.bin").write_bytes(b"b" * 700_000)
    byte_limited = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(path=str(approved), folder_id=folder.id),
    )
    assert "aggregate_byte_limit" in {item.detail for item in byte_limited.items}


def test_visited_and_wall_time_limits_stop_streaming_enumeration(
    db_session: Session,
    user_factory,
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import schemas
    from app.services import document_service

    approved = test_paths.root / "approved"
    approved.mkdir()
    for index in range(5):
        (approved / f"{index}.txt").write_text(str(index), encoding="utf-8")
    _enable(
        monkeypatch,
        document_service,
        approved,
        folder_import_max_visited_entries=1,
    )
    monkeypatch.setattr(
        document_service,
        "ingest_document",
        lambda *_args, **_kwargs: _fake_upload(),
    )
    monkeypatch.setattr(
        document_service.audit_service, "record", lambda *_a, **_k: None
    )
    user, folder = _user_and_folder(db_session, user_factory)

    visited_limited = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(path=str(approved), folder_id=folder.id),
    )
    assert "visited_entry_limit" in {item.detail for item in visited_limited.items}

    monkeypatch.setattr(
        document_service.settings,
        "folder_import_max_visited_entries",
        100,
    )
    ticks = iter([0.0, 2.0, 4.0])
    monkeypatch.setattr(document_service.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(document_service.settings, "folder_import_max_seconds", 1)
    wall_limited = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(path=str(approved), folder_id=folder.id),
    )
    assert "wall_time_limit" in {item.detail for item in wall_limited.items}


def test_import_exception_text_is_not_returned_or_audited(
    db_session: Session,
    user_factory,
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models, schemas
    from app.services import document_service

    approved = test_paths.root / "approved"
    approved.mkdir()
    (approved / "failure.txt").write_text("content", encoding="utf-8")
    _enable(monkeypatch, document_service, approved)
    raw_exception = f"failed while opening {approved}/private-component"

    def fail_ingest(*_args, **_kwargs):
        raise RuntimeError(raw_exception)

    monkeypatch.setattr(document_service, "ingest_document", fail_ingest)
    user, folder = _user_and_folder(db_session, user_factory)
    db_session.commit()

    result = document_service.import_folder(
        db_session,
        user,
        schemas.ImportRequest(path=str(approved), folder_id=folder.id),
    )

    assert result.errors == 1
    assert result.items[0].detail == "import_failed"
    rendered = result.model_dump_json()
    assert raw_exception not in rendered
    assert str(approved) not in rendered
    audit = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "IMPORT_FOLDER")
        .one()
    )
    assert raw_exception not in audit.details
    assert str(approved) not in audit.details
    assert json.loads(audit.details)["source_id"] == result.path
