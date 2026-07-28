"""Bounded, authorization-first text-to-visual retrieval tests."""

from __future__ import annotations

from types import SimpleNamespace

from app import models
from app.config import settings
from app.repositories import visual_search_repository
from app.repositories.visual_search_repository import VisualSearchCandidate
from app.services import visual_access, visual_search_service


def _candidate(
    asset_id: int,
    document_id: int,
    asset_type: str,
    text: str,
    *,
    title: str = "Archive document",
) -> VisualSearchCandidate:
    return VisualSearchCandidate(
        asset_id=asset_id,
        document_id=document_id,
        version_id=document_id,
        title=title,
        asset_type=asset_type,
        page_number=1,
        content_type="image/png",
        checksum=f"{asset_id:064d}"[-64:],
        extraction_text=text,
        extraction_types=("OCR",),
    )


def test_text_to_image_and_page_modes_are_bounded_and_reauthorized(monkeypatch) -> None:
    monkeypatch.setattr(settings, "visual_search_enabled", True)
    candidates = [
        _candidate(1, 10, "IMAGE", "red revenue chart with a legend", title="Quarterly figures"),
        _candidate(2, 20, "IMAGE", "red revenue chart in a restricted file"),
        _candidate(3, 10, "PAGE", "red revenue chart on page one"),
    ]
    monkeypatch.setattr(
        visual_search_repository,
        "list_candidates",
        lambda *args, **kwargs: [
            item
            for item in candidates
            if item.asset_type in kwargs["asset_types"]
        ],
    )
    monkeypatch.setattr(
        visual_search_service,
        "resolve_view_document_ids_for_user_id",
        lambda _db, _user_id: frozenset({10}),
    )
    monkeypatch.setattr(
        visual_access,
        "reauthorize_visual_assets",
        lambda _db, *, user_id, asset_ids: [
            SimpleNamespace(id=asset_id, document_id=10) for asset_id in asset_ids
        ],
    )
    user = models.User(id=7, username="viewer", name="Viewer", email="viewer@example.test")

    images = visual_search_service.search(
        None,
        user,
        query="red revenue chart",
        mode="text_to_image",
        limit=10,
        allowed_ids=frozenset({10, 20}),
    )
    assert images.provider == "visual_text"
    assert [hit.asset_id for hit in images.hits] == [1]
    assert images.hits[0].result_type == "image"

    pages = visual_search_service.search(
        None,
        user,
        query="red revenue chart",
        mode="text_to_page",
        limit=10,
        allowed_ids=frozenset({10}),
    )
    assert [hit.asset_id for hit in pages.hits] == [3]
    assert pages.hits[0].result_type == "page"


def test_visual_search_feature_gate_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "visual_search_enabled", False)
    response = visual_search_service.search(
        None,
        SimpleNamespace(id=7),
        query="anything",
        mode="text_to_image",
        limit=10,
        allowed_ids=frozenset({1}),
    )
    assert response.provider == "disabled"
    assert response.degraded is True
    assert response.hits == []
