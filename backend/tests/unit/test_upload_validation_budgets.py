from __future__ import annotations

import io
import zipfile

import pytest

from app.services.upload_validation import UploadValidationError, validate_upload


def test_malware_hook_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import upload_validation

    upload_validation.set_malware_scanner(lambda _data, _name: True)
    try:
        with pytest.raises(UploadValidationError, match="malware"):
            validate_upload(filename="note.txt", data=b"safe text")
    finally:
        upload_validation.set_malware_scanner(None)


def test_zip_path_traversal_is_rejected() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    with pytest.raises(UploadValidationError, match="unsafe path"):
        validate_upload(filename="bundle.zip", data=payload.getvalue())
