from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.services import lancedb_service, visual_semantic_service
from app.services.visual_semantic_embeddings import Siglip2EmbeddingAdapter


class _FakeBackend:
    def embed_text(self, values: list[str]) -> list[list[float]]:
        assert values == ["a photo of lady in red dress"]
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in values]

    def embed_images(self, values: list[bytes]) -> list[list[float]]:
        assert values == [b"query-image"]
        return [[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in values]


def _adapter() -> Siglip2EmbeddingAdapter:
    return Siglip2EmbeddingAdapter(
        model_path=Path("/tmp/not-loaded-by-fake-backend"),
        model_revision="revision",
        model_sha256="c" * 64,
        dimension=8,
        backend=_FakeBackend(),
    )


def test_text_search_embeds_prompt_and_queries_requested_lane(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "visual_semantic_search_enabled", True)
    visual_semantic_service.set_adapter_for_tests(_adapter())
    seen: list[tuple[str, list[float], frozenset[int], str, int]] = []

    def search_lane(lane, vector, *, authorized_ids, model_revision, limit):
        seen.append((lane, list(vector), authorized_ids, model_revision, limit))
        return [{"asset_id": 41, "document_id": 7, "score": 0.91}]

    monkeypatch.setattr(lancedb_service, "search_visual_semantic", search_lane)
    try:
        result = visual_semantic_service.search_text(
            "lady in red dress",
            mode="text_to_image",
            authorized_ids=frozenset({7}),
            limit=6,
        )
    finally:
        visual_semantic_service.reset_adapter()

    assert result.available is True
    assert result.lane_results["image"][0]["asset_id"] == 41
    assert seen == [("image", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], frozenset({7}), "revision", 6)]


def test_image_search_uses_image_lane_and_never_persists_query_bytes(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "visual_semantic_search_enabled", True)
    visual_semantic_service.set_adapter_for_tests(_adapter())
    captured: list[tuple[str, list[float]]] = []

    def search_lane(lane, vector, *, authorized_ids, model_revision, limit):
        captured.append((lane, list(vector)))
        return [{"asset_id": 42, "document_id": 8, "score": 0.88}]

    monkeypatch.setattr(lancedb_service, "search_visual_semantic", search_lane)
    try:
        result = visual_semantic_service.search_image(
            b"query-image",
            mode="image_to_image",
            authorized_ids=frozenset({8}),
            limit=4,
        )
    finally:
        visual_semantic_service.reset_adapter()

    assert result.available is True
    assert result.lane_results["image"][0]["document_id"] == 8
    assert captured == [("image", [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])]
