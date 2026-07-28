from pathlib import Path

import pytest

from app.services.extraction_service import (
    _effective_tesseract_languages,
    extract_text,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "extraction"


@pytest.mark.parametrize(
    ("filename", "expected_language"),
    (
        ("english.txt", "eng"),
        ("hindi.txt", "hin"),
        ("mixed.txt", "eng+hin"),
    ),
)
def test_supported_language_fixtures_have_measured_quality(
    filename: str,
    expected_language: str,
) -> None:
    source = FIXTURES / filename
    result = extract_text(source, "text/plain", filename=filename)

    assert result.status == "native"
    assert result.language == expected_language
    assert result.extractor_name == "plain-text"
    assert result.confidence is None
    assert result.quality_score is not None
    assert result.quality_score >= 0.5
    assert result.quality_signals["character_count"] > 0
    assert result.quality_signals["language"] == expected_language


def test_ocr_language_selection_records_effective_and_missing_packs() -> None:
    effective, missing = _effective_tesseract_languages({"eng"})

    assert effective == ["eng"]
    assert missing == ["hin"]


@pytest.mark.parametrize(
    "text",
    (
        "\ufffd" * 80,
        ".... ____ |||| " * 40,
        "",
    ),
)
def test_degraded_ocr_signals_remain_bounded_and_explicit(text: str) -> None:
    from app.services.extraction_service import _measure_quality

    score, signals = _measure_quality(text, page_count=1, ocr_confidence=None)
    assert 0.0 <= score <= 1.0
    assert signals["character_count"] == len(text)
    assert signals["ocr_confidence_measured"] is False
