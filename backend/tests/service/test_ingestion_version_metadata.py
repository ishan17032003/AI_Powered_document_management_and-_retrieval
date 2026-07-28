from app.services import document_service


def test_ingestion_version_metadata_is_bounded_and_explicit() -> None:
    versions = {
        "extractor": document_service.EXTRACTOR_VERSION,
        "classifier": document_service.CLASSIFIER_VERSION,
        "chunker": document_service.CHUNKER_VERSION,
        "embedding": document_service.EMBEDDING_VERSION,
        "index": document_service.INDEX_VERSION,
    }
    assert all(1 <= len(value) <= 80 for value in versions.values())
    assert versions["index"] == "fts5-v1"
    assert versions["embedding"] == "disabled-v1"
