from app.services.ingestion_pipeline import IngestionStage, ingestion_stage_plan


def test_ingestion_plan_is_explicit_and_ordered() -> None:
    plan = ingestion_stage_plan()
    assert plan == (
        IngestionStage.EXTRACT,
        IngestionStage.CLASSIFY,
        IngestionStage.DEDUPLICATE,
        IngestionStage.CHUNK,
        IngestionStage.INDEX,
    )
    assert len(plan) == len(set(plan))
