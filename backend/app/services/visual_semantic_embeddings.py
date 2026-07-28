"""Optional local SigLIP 2 image/text embedding adapter.

The dependency-free :mod:`visual_embeddings` adapter remains the deterministic
test double.  This module is the real semantic boundary: it imports the heavy
Transformers/PyTorch stack lazily, accepts only a locally pinned model
directory, and returns normalized vectors with explicit provenance.
"""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, Sequence

from .visual_embeddings import VisualEmbedding


class VisualModelUnavailable(RuntimeError):
    """The semantic model cannot be used in the current runtime profile."""


class VisualEmbeddingBackend(Protocol):
    """Small injectable backend contract used by tests and future runtimes."""

    def embed_text(self, values: Sequence[str]) -> list[list[float]]: ...

    def embed_images(self, values: Sequence[bytes]) -> list[list[float]]: ...


def _weight_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix == ".safetensors" else []
    if not path.is_dir():
        return []
    return sorted(item for item in path.rglob("*.safetensors") if item.is_file())


def artifact_sha256(path: Path) -> str:
    """Hash the primary safetensors file or a deterministic shard manifest."""

    files = _weight_files(path)
    if not files:
        raise VisualModelUnavailable("semantic model has no safetensors weights")
    digest = hashlib.sha256()
    root = path if path.is_dir() else path.parent
    for item in files:
        file_digest = hashlib.sha256()
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(file_digest.digest())
    # A single weight file is commonly distributed with its own published SHA;
    # return that SHA directly so operators can verify the model-card digest.
    if len(files) == 1:
        file_digest = hashlib.sha256()
        with files[0].open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
        return file_digest.hexdigest()
    return digest.hexdigest()


def _as_tensor(output: Any, torch: Any) -> Any:
    if hasattr(output, "detach"):
        return output
    for name in ("text_embeds", "image_embeds", "pooler_output", "last_hidden_state"):
        value = getattr(output, name, None)
        if value is not None and hasattr(value, "detach"):
            if name == "last_hidden_state" and len(value.shape) == 3:
                return value[:, 0, :]
            return value
    if isinstance(output, (tuple, list)) and output and hasattr(output[0], "detach"):
        return output[0]
    raise VisualModelUnavailable("semantic model returned no tensor embedding")


@dataclass(frozen=True, slots=True)
class SemanticModelInfo:
    revision: str
    sha256: str
    dimension: int
    device: str


class Siglip2EmbeddingAdapter:
    """Lazy, local-only SigLIP 2 dual encoder.

    ``model_path`` must point at a pre-staged Hugging Face model directory. The
    adapter never downloads weights and never enables ``trust_remote_code``.
    """

    def __init__(
        self,
        *,
        model_path: Path,
        model_revision: str,
        model_sha256: str,
        dimension: int,
        device: str = "auto",
        max_batch: int = 4,
        backend: VisualEmbeddingBackend | None = None,
    ) -> None:
        if not model_revision or len(model_revision) > 160:
            raise ValueError("semantic model revision is invalid")
        if len(model_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in model_sha256):
            raise ValueError("semantic model SHA-256 is invalid")
        if not 8 <= dimension <= 4096:
            raise ValueError("semantic vector dimension is invalid")
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError("semantic model device is invalid")
        if not 1 <= max_batch <= 64:
            raise ValueError("semantic model batch size is invalid")
        self.model_path = Path(model_path)
        self.info = SemanticModelInfo(
            revision=model_revision,
            sha256=model_sha256.lower(),
            dimension=dimension,
            device=device,
        )
        self.max_batch = max_batch
        self._backend = backend
        self._model: Any | None = None
        self._processor: Any | None = None
        self._torch: Any | None = None
        self._device: Any | None = None
        self._load_lock = threading.Lock()

    @property
    def model_revision(self) -> str:
        return self.info.revision

    @property
    def model_sha256(self) -> str:
        return self.info.sha256

    @property
    def dimension(self) -> int:
        return self.info.dimension

    def _load(self) -> None:
        if self._backend is not None or self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            if not self.model_path.is_dir():
                raise VisualModelUnavailable("semantic model artifact directory is missing")
            if artifact_sha256(self.model_path) != self.model_sha256:
                raise VisualModelUnavailable("semantic model artifact digest mismatch")
            try:
                import torch
                from transformers import AutoModel, AutoProcessor
            except Exception as exc:  # pragma: no cover - exercised in runtime image
                raise VisualModelUnavailable("semantic model dependencies are unavailable") from exc

            if self.info.device == "cuda":
                if not torch.cuda.is_available():
                    raise VisualModelUnavailable("semantic CUDA device is unavailable")
                device = torch.device("cuda")
            elif self.info.device == "cpu":
                device = torch.device("cpu")
            else:
                device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            try:
                processor = AutoProcessor.from_pretrained(
                    str(self.model_path),
                    local_files_only=True,
                )
                model = AutoModel.from_pretrained(
                    str(self.model_path),
                    local_files_only=True,
                    trust_remote_code=False,
                )
                model.eval().to(device)
            except Exception as exc:  # pragma: no cover - depends on model artifact
                raise VisualModelUnavailable("semantic model failed to load") from exc
            self._torch = torch
            self._processor = processor
            self._model = model
            self._device = device

    def _normalize_output(self, output: Any) -> list[list[float]]:
        if self._torch is None:
            raise VisualModelUnavailable("semantic model is not loaded")
        tensor = _as_tensor(output, self._torch)
        if len(tensor.shape) != 2:
            raise VisualModelUnavailable("semantic model returned an invalid shape")
        if int(tensor.shape[1]) != self.dimension:
            raise VisualModelUnavailable("semantic model dimension does not match index contract")
        tensor = self._torch.nn.functional.normalize(tensor.float(), dim=-1)
        values = tensor.detach().cpu().tolist()
        normalized: list[list[float]] = []
        for row in values:
            if not isinstance(row, list) or len(row) != self.dimension:
                raise VisualModelUnavailable("semantic model returned an invalid vector")
            if not all(math.isfinite(float(value)) for value in row):
                raise VisualModelUnavailable("semantic model returned a non-finite vector")
            normalized.append([float(value) for value in row])
        return normalized

    def _embed_text_batch(self, values: Sequence[str]) -> list[list[float]]:
        self._load()
        if self._backend is not None:
            return self._validate_backend_vectors(self._backend.embed_text(values))
        assert self._processor is not None and self._model is not None and self._device is not None
        assert self._torch is not None
        try:
            inputs = self._processor(
                text=list(values),
                padding="max_length",
                return_tensors="pt",
            ).to(self._device)
            with self._torch.no_grad():
                output = self._model.get_text_features(**inputs)
            return self._normalize_output(output)
        except VisualModelUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - depends on model runtime
            raise VisualModelUnavailable("semantic text embedding failed") from exc

    def _embed_image_batch(self, values: Sequence[bytes]) -> list[list[float]]:
        self._load()
        if self._backend is not None:
            return self._validate_backend_vectors(self._backend.embed_images(values))
        assert self._processor is not None and self._model is not None and self._device is not None
        assert self._torch is not None
        try:
            from PIL import Image

            images = []
            for payload in values:
                with Image.open(BytesIO(payload)) as image:
                    image.load()
                    images.append(image.convert("RGB"))
            inputs = self._processor(images=images, return_tensors="pt").to(self._device)
            with self._torch.no_grad():
                output = self._model.get_image_features(**inputs)
            return self._normalize_output(output)
        except VisualModelUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - depends on model runtime
            raise VisualModelUnavailable("semantic image embedding failed") from exc

    def _validate_backend_vectors(self, values: list[list[float]]) -> list[list[float]]:
        if len(values) > self.max_batch:
            raise VisualModelUnavailable("semantic backend exceeded batch bound")
        result: list[list[float]] = []
        for row in values:
            if len(row) != self.dimension:
                raise VisualModelUnavailable("semantic backend dimension mismatch")
            if not all(math.isfinite(float(value)) for value in row):
                raise VisualModelUnavailable("semantic backend returned a non-finite vector")
            norm = math.sqrt(sum(float(value) ** 2 for value in row)) or 1.0
            result.append([float(value) / norm for value in row])
        return result

    def embed_text(self, values: Sequence[str]) -> list[VisualEmbedding]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("semantic text input is empty")
        result: list[VisualEmbedding] = []
        for start in range(0, len(cleaned), self.max_batch):
            rows = self._embed_text_batch(cleaned[start : start + self.max_batch])
            result.extend(
                VisualEmbedding(tuple(row), self.model_revision, self.model_sha256, "text")
                for row in rows
            )
        return result

    def embed_images(self, values: Sequence[bytes], *, modality: str = "image") -> list[VisualEmbedding]:
        if modality not in {"image", "page"}:
            raise ValueError("semantic image modality is invalid")
        if any(not value for value in values):
            raise ValueError("semantic image input is empty")
        result: list[VisualEmbedding] = []
        for start in range(0, len(values), self.max_batch):
            rows = self._embed_image_batch(values[start : start + self.max_batch])
            result.extend(
                VisualEmbedding(tuple(row), self.model_revision, self.model_sha256, modality)
                for row in rows
            )
        return result

    def embed(self, payload: bytes, *, modality: str) -> VisualEmbedding:
        if modality == "text":
            return self.embed_text([payload.decode("utf-8")])[0]
        return self.embed_images([payload], modality=modality)[0]


__all__ = [
    "SemanticModelInfo",
    "Siglip2EmbeddingAdapter",
    "VisualEmbeddingBackend",
    "VisualModelUnavailable",
    "artifact_sha256",
]
