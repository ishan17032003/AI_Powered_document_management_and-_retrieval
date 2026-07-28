"""HTTP coverage for the public text-to-visual search contract."""

from __future__ import annotations

from io import BytesIO

import fitz
from httpx import Response
from PIL import Image, ImageDraw
from starlette.testclient import TestClient

from app import schemas


def _expect(response: Response, status_code: int) -> Response:
    assert response.status_code == status_code, response.text
    return response


def _pdf_with_text_and_image() -> bytes:
    image_output = BytesIO()
    image = Image.new("RGB", (180, 100), "#e3edff")
    ImageDraw.Draw(image).rectangle((20, 20, 160, 80), outline="#334d9a", width=4)
    image.save(image_output, format="PNG")

    document = fitz.open()
    page = document.new_page(width=420, height=300)
    page.insert_text((48, 60), "Quarterly revenue chart and invoice summary")
    page.insert_image(fitz.Rect(120, 110, 300, 210), stream=image_output.getvalue())
    payload = document.tobytes()
    document.close()
    return payload


def _run_ingestion(uploaded: dict) -> None:
    from app import database
    from app.repositories import job_repository
    from app.services import ingestion_worker

    db = database.SessionLocal()
    try:
        claimed = job_repository.claim_ingestion_job(
            db,
            owner=f"visual-search-{uploaded['job_id']}",
        )
        assert claimed is not None
        db.commit()
        completed = ingestion_worker.run_claimed_job(db, claimed)
        assert completed.state in {"SUCCEEDED", "REVIEW"}
    finally:
        db.close()


def test_text_to_visual_route_exposes_typed_modes_and_result_lineage(
    admin_client: TestClient,
    monkeypatch,
) -> None:
    expected = schemas.VisualSearchResponse(
        query="revenue chart",
        mode="text_to_image",
        count=1,
        provider="visual_text",
        hits=[
            schemas.VisualSearchHit(
                asset_id=41,
                document_id=7,
                version_id=9,
                title="Quarterly figures",
                asset_type="IMAGE",
                result_type="image",
                page_number=1,
                content_type="image/png",
                snippet="Revenue chart",
                score=0.75,
                matched_lanes=["image"],
            )
        ],
    )
    monkeypatch.setattr(
        "app.services.search_application_service.run_visual_search",
        lambda *args, **kwargs: expected,
    )

    response = _expect(
        admin_client.post(
            "/api/v1/search/visual",
            json={"q": "revenue chart", "mode": "text_to_image", "limit": 10},
        ),
        200,
    ).json()
    assert response["mode"] == "text_to_image"
    assert response["provider"] == "visual_text"
    assert response["hits"][0]["asset_id"] == 41
    assert response["hits"][0]["result_type"] == "image"

    invalid = admin_client.post(
        "/api/v1/search/visual",
        json={"q": "page", "mode": "unsupported", "limit": 10},
    )
    assert invalid.status_code == 422


def test_ask_ai_route_remains_available_after_visual_search_wiring(
    admin_client: TestClient,
) -> None:
    response = _expect(
        admin_client.post(
            "/api/v1/search/ask",
            json={"question": "What visual evidence is in the archive?"},
        ),
        200,
    ).json()
    assert isinstance(response["answer"], str)
    assert isinstance(response["citations"], list)


def test_text_to_page_and_image_search_after_pdf_ingestion(
    admin_client: TestClient,
) -> None:
    uploaded = _expect(
        admin_client.post(
            "/api/v1/documents",
            files={
                "file": (
                    "revenue-report.pdf",
                    _pdf_with_text_and_image(),
                    "application/pdf",
                )
            },
        ),
        202,
    ).json()
    _run_ingestion(uploaded)

    page_result = _expect(
        admin_client.post(
            "/api/v1/search/visual",
            json={"q": "revenue chart", "mode": "text_to_page", "limit": 10},
        ),
        200,
    ).json()
    assert page_result["provider"] == "visual_text"
    assert page_result["count"] >= 1
    assert page_result["hits"][0]["result_type"] == "page"

    image_result = _expect(
        admin_client.post(
            "/api/v1/search/visual",
            json={"q": "revenue chart", "mode": "text_to_image", "limit": 10},
        ),
        200,
    ).json()
    assert image_result["count"] >= 1
    assert image_result["hits"][0]["result_type"] == "image"
    preview = _expect(
        admin_client.get(
            f"/api/v1/search/visual-assets/{image_result['hits'][0]['asset_id']}/preview"
        ),
        200,
    )
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.content
