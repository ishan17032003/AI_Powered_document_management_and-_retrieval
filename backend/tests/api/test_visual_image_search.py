from __future__ import annotations

from io import BytesIO

from PIL import Image
from starlette.testclient import TestClient

from app import schemas


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), "#b51f3a").save(output, format="PNG")
    return output.getvalue()


def test_visual_image_route_returns_typed_semantic_results(
    admin_client: TestClient,
    monkeypatch,
) -> None:
    expected = schemas.VisualSearchResponse(
        query="[uploaded image]",
        mode="image_to_image",
        count=1,
        provider="siglip2",
        hits=[
            schemas.VisualSearchHit(
                asset_id=19,
                document_id=7,
                version_id=11,
                title="Wardrobe reference",
                asset_type="IMAGE",
                result_type="image",
                content_type="image/png",
                snippet="",
                score=0.94,
                matched_lanes=["image"],
            )
        ],
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.visual_assets.validate_visual_bytes_isolated",
        lambda payload, content_type: captured.update({"validated": (payload, content_type)}) or object(),
    )
    monkeypatch.setattr(
        "app.services.visual_assets.normalize_visual_derivative_isolated",
        lambda payload, content_type, output_format, max_output_bytes: captured.update(
            {"normalized": (payload, content_type, output_format, max_output_bytes)}
        ) or payload,
    )
    monkeypatch.setattr(
        "app.services.search_application_service.run_visual_image_search",
        lambda *args, **kwargs: expected,
    )

    response = admin_client.post(
        "/api/v1/search/visual/image?mode=image_to_image&limit=10",
        files={"image": ("query.png", _png(), "image/png")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "siglip2"
    assert body["mode"] == "image_to_image"
    assert body["hits"][0]["asset_id"] == 19
    assert captured["validated"][1] == "image/png"
    assert captured["normalized"][2] == "PNG"


def test_visual_image_route_rejects_unsupported_mode(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/api/v1/search/visual/image?mode=text_to_page",
        files={"image": ("query.png", _png(), "image/png")},
    )

    assert response.status_code == 422


def test_visual_image_route_enforces_bounded_query_bytes(
    admin_client: TestClient,
    monkeypatch,
) -> None:
    async def reject_large_upload(*args, **kwargs):
        raise ValueError("image query exceeds byte budget")

    monkeypatch.setattr(
        "app.routers.search._read_bounded_upload",
        reject_large_upload,
    )
    response = admin_client.post(
        "/api/v1/search/visual/image",
        files={"image": ("query.png", b"0123456789", "image/png")},
    )

    assert response.status_code == 400
