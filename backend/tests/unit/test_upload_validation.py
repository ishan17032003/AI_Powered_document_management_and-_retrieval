"""Hostile and mismatch fixtures for the upload quarantine preflight."""

import pytest

from app.services.upload_validation import UploadValidationError, validate_upload


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        ("../escape.pdf", b"%PDF-1.7", "application/pdf"),
        ("invoice.pdf", b"not-a-pdf", "application/pdf"),
        ("invoice.pdf", b"%PDF-1.7", "image/png"),
        ("report.exe", b"MZ\x90\x00", "application/octet-stream"),
        ("bad\x00name.txt", b"plain text", "text/plain"),
    ],
)
def test_hostile_or_mismatched_uploads_are_rejected(
    filename: str, data: bytes, content_type: str
) -> None:
    with pytest.raises(UploadValidationError):
        validate_upload(filename=filename, data=data, content_type=content_type)


def test_valid_pdf_is_normalized_and_accepted() -> None:
    result = validate_upload(
        filename="  invoice.PDF ",
        data=b"%PDF-1.7\nbody",
        content_type="application/pdf; charset=binary",
    )
    assert result.filename == "invoice.PDF"
    assert result.extension == ".pdf"
    assert result.content_type == "application/pdf"


def test_eicar_fixture_is_rejected_before_extraction() -> None:
    with pytest.raises(UploadValidationError, match="malware detected"):
        validate_upload(
            filename="sample.txt",
            data=b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*",
            content_type="text/plain",
        )


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        ("audio.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00audio-data", "audio/mpeg"),
        ("audio.wav", b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00", "audio/wav"),
        ("audio.ogg", b"OggS\x00\x02\x00\x00\x00\x00\x00\x00", "audio/ogg"),
        ("audio.flac", b"fLaC\x00\x00\x00\x22", "audio/flac"),
        ("audio.aac", b"\xff\xf1\x50\x80audio-aac", "audio/aac"),
        ("video.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", "video/mp4"),
        ("video.avi", b"RIFF\x24\x00\x00\x00AVI LIST\x00\x00\x00\x00", "video/x-msvideo"),
        ("video.mov", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
        ("video.mkv", b"\x1a\x45\xdf\xa3\x93\x42\x86\x81\x01\x42\xf7\x81\x01", "video/x-matroska"),
        ("video.webm", b"\x1a\x45\xdf\xa3\x93\x42\x86\x81\x01\x42\xf7\x81\x01", "video/webm"),
        ("subtitles.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello world\n", "text/vtt"),
    ],
)
def test_audio_video_and_vtt_uploads_accepted(
    filename: str, data: bytes, content_type: str
) -> None:
    result = validate_upload(filename=filename, data=data, content_type=content_type)
    assert result.filename == filename
    assert result.content_type == content_type


def test_large_media_upload_bypasses_size_limit() -> None:
    # 60 MB audio payload (above default 50 MB limit)
    large_audio_payload = b"ID3" + (b"\x00" * (60 * 1024 * 1024))
    result = validate_upload(
        filename="large_recording.mp3",
        data=large_audio_payload,
        content_type="audio/mpeg",
    )
    assert result.filename == "large_recording.mp3"
    assert result.extension == ".mp3"

