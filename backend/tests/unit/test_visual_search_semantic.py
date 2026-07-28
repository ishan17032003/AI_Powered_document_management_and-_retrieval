from __future__ import annotations

from types import SimpleNamespace

from app.config import settings
from app.repositories import visual_search_repository
from app.repositories.visual_search_repository import VisualSearchCandidate
from app.services import (
    visual_access,
    visual_search_service,
    visual_semantic_service,
)


def test_text_search_returns_siglip2_hits_after_hydration_and_reauthorization(
    monkeypatch,
) -> None:
    candidate = VisualSearchCandidate(
        asset_id=41,
        document_id=7,
        version_id=9,
        title="Wardrobe reference",
        asset_type="IMAGE",
        page_number=1,
        content_type="image/png",
        checksum="d" * 64,
        extraction_text="red dress reference",
        extraction_types=("OCR",),
    )
    semantic = visual_semantic_service.SemanticSearchResult(
        available=True,
        provider="siglip2",
        lane_results={
            "image": [
                {
                    "asset_id": 41,
                    "document_id": 7,
                    "score": 0.93,
                    "result_type": "image",
                }
            ],
            "page": [],
        },
    )
    monkeypatch.setattr(settings, "visual_search_enabled", True)
    monkeypatch.setattr(
        visual_semantic_service,
        "search_text",
        lambda *args, **kwargs: semantic,
    )
    monkeypatch.setattr(
        visual_search_repository,
        "candidates_by_asset_ids",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(
        visual_search_service,
        "resolve_view_document_ids_for_user_id",
        lambda *args, **kwargs: frozenset({7}),
    )
    monkeypatch.setattr(
        visual_access,
        "reauthorize_visual_assets",
        lambda *args, **kwargs: [SimpleNamespace(id=41)],
    )

    response = visual_search_service.search(
        SimpleNamespace(),
        SimpleNamespace(id=1),
        query="lady in red dress",
        mode="text_to_image",
        limit=10,
        allowed_ids=frozenset({7}),
    )

    assert response.provider == "siglip2"
    assert response.count == 1
    assert response.hits[0].asset_id == 41
    assert response.hits[0].score == 0.93
    assert response.hits[0].matched_lanes == ["image"]


def test_image_search_falls_back_to_exact_content_addressed_match(
    monkeypatch,
) -> None:
    candidate = VisualSearchCandidate(
        asset_id=51,
        document_id=7,
        version_id=9,
        title="Exact uploaded reference",
        asset_type="IMAGE",
        page_number=1,
        content_type="image/png",
        checksum="e" * 64,
        extraction_text="reference image",
        extraction_types=("OCR",),
    )
    monkeypatch.setattr(settings, "visual_search_enabled", True)
    monkeypatch.setattr(
        visual_semantic_service,
        "search_image",
        lambda *args, **kwargs: visual_semantic_service.SemanticSearchResult(
            available=False,
            lane_results={},
            degraded=True,
            error_code="VISUAL_MODEL_UNAVAILABLE",
        ),
    )
    monkeypatch.setattr(
        visual_search_repository,
        "candidates_by_checksum",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(
        visual_search_service,
        "resolve_view_document_ids_for_user_id",
        lambda *args, **kwargs: frozenset({7}),
    )
    monkeypatch.setattr(
        visual_access,
        "reauthorize_visual_assets",
        lambda *args, **kwargs: [SimpleNamespace(id=51)],
    )

    response = visual_search_service.search_image(
        SimpleNamespace(),
        SimpleNamespace(id=1),
        payload=b"query",
        mode="image_to_image",
        limit=10,
        allowed_ids=frozenset({7}),
    )

    assert response is not None
    assert response.provider == "sql_exact"
    assert response.degraded is True
    assert response.hits[0].asset_id == 51
