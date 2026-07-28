from app.services.extraction_service import _measure_quality


def test_extraction_quality_uses_measured_signals_without_fabricated_confidence() -> None:
    score, signals = _measure_quality(
        "Readable extracted text", page_count=1, ocr_confidence=None
    )
    assert 0.0 <= score <= 1.0
    assert signals["ocr_confidence_measured"] is False
    assert signals["character_count"] == 23


def test_ocr_quality_incorporates_bounded_provider_confidence() -> None:
    score, signals = _measure_quality(
        "text", page_count=1, ocr_confidence=1.5
    )
    assert 0.0 <= score <= 1.0
    assert signals["ocr_confidence_measured"] is True
