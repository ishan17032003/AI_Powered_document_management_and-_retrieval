"""STORE-001/002 local ObjectStore contract coverage."""

import io
from hashlib import sha256

import pytest

from app.storage import FilesystemObjectStore


def test_stage_promote_is_checksum_addressed_and_idempotent(tmp_path):
    store = FilesystemObjectStore(tmp_path)
    payload = b"immutable-object"
    checksum = sha256(payload).hexdigest()
    staged = store.stage(io.BytesIO(payload), suffix=".txt")
    key = store.promote(staged.key, checksum=checksum)
    assert key == f"objects/{checksum[:2]}/{checksum}"
    assert store.verify(key, checksum=checksum)

    second = store.stage(io.BytesIO(payload))
    assert store.promote(second.key, checksum=checksum) == key
    with pytest.raises(IOError):
        store.promote(store.stage(io.BytesIO(b"other")).key, checksum=checksum)


def test_quarantine_and_tombstone_are_state_moves(tmp_path):
    store = FilesystemObjectStore(tmp_path)
    payload = b"stateful-object"
    checksum = sha256(payload).hexdigest()
    key = store.promote(store.stage(io.BytesIO(payload)).key, checksum=checksum)
    quarantined = store.quarantine(key)
    assert quarantined.startswith(".quarantine/")
    assert not (tmp_path / key).exists()
