import json

from starlette.testclient import TestClient


def test_worker_persists_provenance_and_only_then_marks_ready(
    admin_client: TestClient,
) -> None:
    response = admin_client.post(
        "/api/v1/documents",
        files={
            "file": (
                "worker-english.txt",
                (
                    b"Approved English document content with sufficient readable "
                    b"characters for measured extraction quality."
                ),
                "text/plain",
            )
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    from app import database
    from app.repositories import job_repository
    from app.services import ingestion_pipeline, ingestion_worker

    db = database.SessionLocal()
    try:
        claimed = job_repository.claim_ingestion_job(
            db,
            owner="test-ingestion-worker",
        )
        assert claimed is not None
        assert claimed.id == job_id
        db.commit()

        completed = ingestion_worker.run_claimed_job(db, claimed)
        db.refresh(completed)
        assert completed.state == "SUCCEEDED"

        document = completed.document
        version = completed.version
        assert document is not None
        assert version is not None
        assert document.status == "READY"
        assert document.ocr_status == "native"
        assert document.ocr_confidence is None
        assert document.language == "eng"
        assert version.extractor_name == "plain-text"
        assert version.extractor_version
        assert version.extraction_quality_score is not None
        assert version.extraction_completed_at is not None
        assert json.loads(version.extraction_quality_signals)["language"] == "eng"

        results = ingestion_pipeline.stage_results(completed)
        assert all(
            results[stage.value]["status"] == "completed"
            for stage in ingestion_pipeline.ingestion_stage_plan()
        )
        assert results["VECTOR_INDEXING"]["status"] == "disabled"
        assert ingestion_pipeline.degraded_stages(completed) == []
    finally:
        db.close()

    status = admin_client.get(f"/api/v1/ingestions/{job_id}")
    assert status.status_code == 200, status.text
    extraction = status.json()["extraction"]
    assert extraction["method"] == "native"
    assert extraction["extractor_name"] == "plain-text"
    assert extraction["quality_score"] is not None
    assert extraction["quality_signals"]["language"] == "eng"
