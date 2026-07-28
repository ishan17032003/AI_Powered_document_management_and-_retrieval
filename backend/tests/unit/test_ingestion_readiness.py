import json

from app import models
from app.services import ingestion_pipeline
from app.services.ingestion_worker import INDEX_VERSION, mandatory_stages_complete


def _ready_inputs():
    document = models.Document(
        status="PROCESSING",
        ocr_status="native",
        page_count=1,
    )
    version = models.DocVersion(
        storage_state="AVAILABLE",
        ocr_text="text",
        index_version=INDEX_VERSION,
    )
    job = models.IngestionJob(
        id="job-ready",
        idempotency_key="job-ready",
        stage="INDEX",
        stage_results="{}",
        degraded_stages="[]",
    )
    for stage in ingestion_pipeline.ingestion_stage_plan():
        ingestion_pipeline.record_stage_result(
            job,
            stage,
            ingestion_pipeline.StageResultStatus.COMPLETED,
        )
    ingestion_pipeline.record_optional_stage(
        job,
        "VECTOR_INDEXING",
        ingestion_pipeline.StageResultStatus.DISABLED,
    )
    return document, version, job


def test_ready_requires_every_mandatory_stage() -> None:
    document, version, job = _ready_inputs()
    assert mandatory_stages_complete(document, version, job)

    results = ingestion_pipeline.stage_results(job)
    results.pop(ingestion_pipeline.IngestionStage.CHUNK.value)

    job.stage_results = json.dumps(results)
    assert not mandatory_stages_complete(document, version, job)


def test_enabled_optional_stage_degradation_prevents_false_ready() -> None:
    document, version, job = _ready_inputs()
    ingestion_pipeline.record_optional_stage(
        job,
        "VECTOR_INDEXING",
        ingestion_pipeline.StageResultStatus.DEGRADED,
        code="VECTOR_INDEX_UNAVAILABLE",
    )

    decision = ingestion_pipeline.evaluate_readiness(job)
    assert not decision.ready
    assert decision.review_required
    assert decision.degraded_stages == ("VECTOR_INDEXING",)
    assert not mandatory_stages_complete(document, version, job)
