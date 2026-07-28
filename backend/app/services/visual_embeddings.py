"""Pinned, deterministic visual embedding contracts for page/image lanes."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VisualEmbedding:
    vector: tuple[float, ...]
    model_revision: str
    model_sha256: str
    modality: str


class PinnedVisualEmbeddingAdapter:
    """Dependency-free contract adapter; semantic models remain MM-004 gated."""

    def __init__(self, *, model_revision: str, model_sha256: str, dimension: int = 128) -> None:
        if not model_revision or len(model_revision) > 160 or len(model_sha256) != 64:
            raise ValueError("visual model provenance is invalid")
        if not 8 <= dimension <= 4096:
            raise ValueError("visual embedding dimension is invalid")
        self.model_revision = model_revision
        self.model_sha256 = model_sha256.lower()
        self.dimension = dimension

    def embed(self, payload: bytes, *, modality: str) -> VisualEmbedding:
        if modality not in {"image", "page"} or not payload:
            raise ValueError("visual embedding input is invalid")
        seed = hashlib.sha256(self.model_sha256.encode() + b":" + payload).digest()
        values: list[float] = []
        counter = 0
        while len(values) < self.dimension:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            values.extend((byte / 127.5) - 1.0 for byte in block)
            counter += 1
        values = values[: self.dimension]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return VisualEmbedding(tuple(value / norm for value in values), self.model_revision, self.model_sha256, modality)


__all__ = ["PinnedVisualEmbeddingAdapter", "VisualEmbedding"]
