from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.services.visual_semantic_embeddings import (
    Siglip2EmbeddingAdapter,
    VisualModelUnavailable,
    artifact_sha256,
)


class _FakeBackend:
    def embed_text(self, values: list[str]) -> list[list[float]]:
        return [[3.0, 4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in values]

    def embed_images(self, values: list[bytes]) -> list[list[float]]:
        return [[0.0, 0.0, 5.0, 12.0, 0.0, 0.0, 0.0, 0.0] for _ in values]


def test_local_adapter_normalizes_vectors_and_keeps_model_provenance() -> None:
    adapter = Siglip2EmbeddingAdapter(
        model_path=Path("/tmp/not-loaded-by-fake-backend"),
        model_revision="75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2",
        model_sha256="a" * 64,
        dimension=8,
        max_batch=2,
        backend=_FakeBackend(),
    )

    text = adapter.embed_text(["a photo of a lady in a red dress"])[0]
    image = adapter.embed_images([b"png"], modality="image")[0]

    assert text.vector == pytest.approx((0.6, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    assert image.vector == pytest.approx((0.0, 0.0, 5 / 13, 12 / 13, 0.0, 0.0, 0.0, 0.0))
    assert text.model_revision == adapter.model_revision
    assert text.model_sha256 == "a" * 64
    assert text.modality == "text"
    assert image.modality == "image"


def test_adapter_rejects_invalid_backend_dimension() -> None:
    class BadBackend(_FakeBackend):
        def embed_text(self, values: list[str]) -> list[list[float]]:
            return [[1.0, 2.0, 3.0] for _ in values]

    adapter = Siglip2EmbeddingAdapter(
        model_path=Path("/tmp/not-loaded-by-fake-backend"),
        model_revision="revision",
        model_sha256="b" * 64,
        dimension=8,
        backend=BadBackend(),
    )

    with pytest.raises(VisualModelUnavailable, match="dimension"):
        adapter.embed_text(["query"])


def test_artifact_sha256_hashes_a_single_safetensors_file(tmp_path: Path) -> None:
    weights = tmp_path / "model.safetensors"
    payload = b"deterministic test weights"
    weights.write_bytes(payload)

    assert artifact_sha256(tmp_path) == hashlib.sha256(payload).hexdigest()


def test_artifact_sha256_requires_safetensors_weights(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(VisualModelUnavailable, match="safetensors"):
        artifact_sha256(tmp_path)
