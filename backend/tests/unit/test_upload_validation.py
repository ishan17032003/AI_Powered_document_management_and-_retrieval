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
