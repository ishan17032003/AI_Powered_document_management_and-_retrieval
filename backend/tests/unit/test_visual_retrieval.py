import pytest

from app.services.lancedb_service import visual_index_schema
from app.services.visual_embeddings import PinnedVisualEmbeddingAdapter
from app.services.visual_retrieval import (
    LaneBudget,
    VisualLane,
    VisualQueryMode,
    reciprocal_rank_fusion,
    route_typed_query,
)


def _hit(document_id: int, asset_id: int, score: float, lane: str = "page") -> dict:
    return {"document_id": document_id, "asset_id": asset_id, "score": score, "snippet": lane}


def test_visual_query_modes_are_bounded_and_rank_fused() -> None:
    allowed = frozenset({1, 2})
    calls: list[VisualLane] = []

    def page(_query: str, _ids: frozenset[int], budget: int) -> list[dict]:
        calls.append(VisualLane.PAGE)
        assert budget == 2
        return [_hit(1, 11, 0.2), _hit(2, 12, 0.9), _hit(99, 99, 1.0)]

    result = route_typed_query(
        mode=VisualQueryMode.HYBRID,
        query="table",
        allowed_ids=allowed,
        limit=10,
        lane_searchers={VisualLane.PAGE: page},
        budgets=LaneBudget(page=2),
    )
    assert calls == [VisualLane.PAGE]
    assert {item["document_id"] for item in result} == {1, 2}
    assert result[0]["fusion_score"] > 0


def test_visual_fusion_never_adds_raw_incompatible_scores() -> None:
    result = reciprocal_rank_fusion(
        {VisualLane.PAGE: [_hit(1, 11, 0.01)], VisualLane.IMAGE: [_hit(1, 11, 1000.0)]},
        limit=5,
        allowed_ids=frozenset({1}),
    )
    assert result[0]["fusion_score"] == round(2 / 61, 8)
    assert result[0]["matched_lanes"] == ["image", "page"]


def test_pinned_visual_embedding_is_repeatable_and_versioned() -> None:
    adapter = PinnedVisualEmbeddingAdapter(model_revision="visual-test-v1", model_sha256="a" * 64, dimension=16)
    first = adapter.embed(b"page-bytes", modality="page")
    second = adapter.embed(b"page-bytes", modality="page")
    assert first == second
    assert len(first.vector) == 16
    assert abs(sum(value * value for value in first.vector) - 1.0) < 1e-6
    assert first.model_revision == "visual-test-v1"


def test_visual_lance_schema_has_separate_page_and_image_lanes() -> None:
    contract = visual_index_schema(dimension=16)
    assert "vectors" in contract["visual_pages"]
    assert "vector" in contract["image_assets"]
    assert contract["dimension"] == 16


def test_pinned_visual_embedding_rejects_unverifiable_model_provenance() -> None:
    with pytest.raises(ValueError):
        PinnedVisualEmbeddingAdapter(model_revision="v1", model_sha256="not-a-digest")


def test_visual_embedding_records_modality_and_model_digest() -> None:
    adapter = PinnedVisualEmbeddingAdapter(model_revision="v1", model_sha256="b" * 64, dimension=8)
    result = adapter.embed(b"image-bytes", modality="image")
    assert result.modality == "image"
    assert result.model_sha256 == "b" * 64
