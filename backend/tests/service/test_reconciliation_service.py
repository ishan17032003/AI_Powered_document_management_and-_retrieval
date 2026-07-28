"""Read-only reconciliation drift classification."""

from __future__ import annotations

import io

import pytest

from app import models
from app.repositories.search_repository import index_fts
from app.retrieval_store import Fts5RetrievalStore
from app.services.reconciliation_service import reconcile
from app.storage import FilesystemObjectStore


def test_reconcile_classifies_checksum_and_missing_object(db_session, user_factory, tmp_path):
    user = user_factory()
    cabinet = models.Cabinet(name="Cabinet")
    db_session.add(cabinet)
    db_session.flush()
    folder = models.Folder(cabinet_id=cabinet.id, name="Folder")
    db_session.add(folder)
    db_session.flush()
    document = models.Document(
        folder_id=folder.id,
        title="Report",
        content_hash="a" * 64,
        created_by=user.id,
        lifecycle_state="ACTIVE",
    )
    db_session.add(document)
    db_session.flush()
    store = FilesystemObjectStore(tmp_path)
    staged = store.stage(io.BytesIO(b"actual"))
    key = store.promote(staged.key, checksum=staged.checksum)
    db_session.add(
        models.DocVersion(
            document_id=document.id,
            version_no=1,
            file_key=key,
            filename="report.txt",
            checksum="b" * 64,
            size=6,
            created_by=user.id,
            storage_state="AVAILABLE",
        )
    )
    db_session.flush()
    report = reconcile(db_session, tmp_path)
    assert report["read_only"] is True
    assert report["summary"]["checksum_mismatch"] == 1
    assert report["summary"]["missing_index"] == 1


def test_reconcile_classifies_orphan_and_stale_fts(db_session, tmp_path):
    store = FilesystemObjectStore(tmp_path)
    staged = store.stage(io.BytesIO(b"orphan"))
    store.promote(staged.key, checksum=staged.checksum)
    index_fts(db_session, 9999, "stale", "stale text")
    db_session.flush()
    report = reconcile(db_session, tmp_path)
    assert report["summary"]["orphan_object"] == 1
    assert report["summary"]["stale_index"] == 1


def test_reconcile_rejects_unbounded_limit(db_session, tmp_path):
    with pytest.raises(ValueError):
        reconcile(db_session, tmp_path, max_items=0)


def test_reconcile_includes_retrieval_store_contract_report(db_session, tmp_path):
    report = reconcile(db_session, tmp_path, retrieval_stores=(Fts5RetrievalStore(db_session),))
    assert report["retrieval_stores"]["fts5"]["checked"] is True
