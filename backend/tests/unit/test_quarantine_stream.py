from __future__ import annotations

from io import BytesIO

import pytest

from app.services.quarantine import stage_stream


def test_stage_stream_hashes_without_loading_whole_upload(tmp_path) -> None:
    staged = stage_stream(BytesIO(b"hello"), directory=tmp_path, max_bytes=5)
    assert staged.size == 5
    assert staged.checksum == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert staged.path.read_bytes() == b"hello"


def test_stage_stream_removes_partial_file_when_limit_is_exceeded(tmp_path) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        stage_stream(BytesIO(b"toolong"), directory=tmp_path, max_bytes=5)
    assert list(tmp_path.iterdir()) == []
