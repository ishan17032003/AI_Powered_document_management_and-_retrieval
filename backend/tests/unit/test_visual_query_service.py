from io import BytesIO

from PIL import Image

from app.services.visual_query_service import run_ephemeral_image_query


def _png() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (3, 2), (10, 20, 30)).save(stream, format="PNG")
    return stream.getvalue()


def test_image_query_is_bounded_ephemeral_and_lancedb_empty_without_match() -> None:
    result = run_ephemeral_image_query(_png(), "image/png", limit=5)
    assert result.count == 0
    assert result.hits == []
    assert result.provider == "lancedb_empty"
    assert result.audit["retention"] == "ephemeral"
