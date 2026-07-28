from types import SimpleNamespace

from app import schemas
from app.services import document_service


def _result(document_id: int = 41) -> schemas.UploadResult:
    return schemas.UploadResult(
        id=document_id,
        title="replayed.pdf",
        status="READY",
        folder_id=1,
        ocr_status="native",
        pipeline={"index": "replay"},
    )


def test_idempotent_upload_replays_existing_document_without_ingest(monkeypatch) -> None:
    job = SimpleNamespace(document_id=41)
    document = SimpleNamespace(
        id=41,
        title="replayed.pdf",
        status="READY",
        folder_id=1,
        ocr_status="native",
        doc_class=None,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        document_service.job_repository,
        "create_ingestion_job",
        lambda *args, **kwargs: job,
    )
    monkeypatch.setattr(
        document_service.document_repository,
        "get",
        lambda *args, **kwargs: document,
    )
    monkeypatch.setattr(
        document_service,
        "ingest_document",
        lambda *args, **kwargs: calls.append("ingest") or _result(99),
    )

    result = document_service.ingest_document_idempotent(
        object(),
        object(),
        idempotency_key="upload-replay",
        filename="new.pdf",
        data=b"new",
        content_type="application/pdf",
    )

    assert result.id == 41
    assert result.pipeline == {"index": "replay", "dedupCheck": "replayed"}
    assert calls == []


def test_idempotent_upload_records_document_after_first_ingest(monkeypatch) -> None:
    job = SimpleNamespace(document_id=None, state="PENDING", stage="EXTRACT", completed_at=None)
    commits: list[bool] = []

    class DB:
        def commit(self):
            commits.append(True)

    monkeypatch.setattr(
        document_service.job_repository,
        "create_ingestion_job",
        lambda *args, **kwargs: job,
    )
    monkeypatch.setattr(
        document_service,
        "ingest_document",
        lambda *args, **kwargs: _result(42),
    )

    result = document_service.ingest_document_idempotent(
        DB(),
        object(),
        idempotency_key="upload-first",
        filename="first.pdf",
        data=b"first",
        content_type="application/pdf",
    )

    assert result.id == 42
    assert job.document_id == 42
    assert job.state == "PENDING"
    assert job.stage == "EXTRACT"
    assert job.completed_at is None
    assert commits == [True]
