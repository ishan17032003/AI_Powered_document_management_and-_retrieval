"""Unit tests for media and WebVTT extraction in extraction_service."""

from pathlib import Path
from tempfile import NamedTemporaryFile

from app.services.extraction_service import (
    _extract_media_fallback,
    _extract_vtt,
    extract_text,
)


def test_vtt_extraction() -> None:
    vtt_content = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
Welcome to XENIUS DocVault.

2
00:00:05.000 --> 00:00:09.000
<v Speaker>This is a test transcript for video and audio support.</v>
"""
    with NamedTemporaryFile(suffix=".vtt", mode="w", encoding="utf-8", delete=False) as f:
        f.write(vtt_content)
        path = Path(f.name)

    try:
        res = _extract_vtt(path)
        assert res.status == "native"
        assert "Welcome to XENIUS DocVault." in res.text
        assert "This is a test transcript for video and audio support." in res.text
        assert "WEBVTT" not in res.text
        assert "00:00:01.000" not in res.text
    finally:
        path.unlink(missing_ok=True)


def test_media_fallback_extraction() -> None:
    with NamedTemporaryFile(suffix=".mp3", mode="wb", delete=False) as f:
        f.write(b"ID3\x03\x00\x00\x00\x00\x00\x00audio-data")
        path = Path(f.name)

    try:
        res = _extract_media_fallback(path, ".mp3")
        assert res.status in ("native", "ocr")
        assert res.extractor_name in ("media-asr", "docling")
        assert len(res.text) > 0
    finally:
        path.unlink(missing_ok=True)


def test_extract_text_routes_vtt() -> None:
    vtt_content = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello DocVault\n"
    with NamedTemporaryFile(suffix=".vtt", mode="w", encoding="utf-8", delete=False) as f:
        f.write(vtt_content)
        path = Path(f.name)

    try:
        res = extract_text(path, filename="meeting.vtt")
        assert res.status == "native"
        assert "Hello DocVault" in res.text
    finally:
        path.unlink(missing_ok=True)
