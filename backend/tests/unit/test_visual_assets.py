from io import BytesIO

import pytest
from PIL import Image

from app.services.visual_assets import (
    VisualValidationError,
    content_addressed_derivative_key,
    deterministic_asset_key,
    embedded_region_lineage,
    normalize_visual_derivative,
    normalize_visual_derivative_isolated,
    validate_visual_bytes,
    validate_visual_bytes_isolated,
)


def _png(size: tuple[int, int] = (4, 3)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


def test_visual_validation_returns_checksum_dimensions_and_phash() -> None:
    result = validate_visual_bytes(_png(), "image/png")
    assert len(result.checksum) == 64
    assert len(result.perceptual_hash) == 16
    assert (result.width, result.height, result.size) == (4, 3, len(_png()))


def test_visual_validation_rejects_wrong_type_and_malformed_bytes() -> None:
    with pytest.raises(VisualValidationError):
        validate_visual_bytes(_png(), "application/pdf")
    with pytest.raises(VisualValidationError):
        validate_visual_bytes(b"not-an-image", "image/png")


def test_asset_key_is_stable_and_scoped_to_version_and_page() -> None:
    checksum = "a" * 64
    assert deterministic_asset_key(7, "PAGE", checksum, 1) == deterministic_asset_key(7, "PAGE", checksum, 1)
    assert deterministic_asset_key(7, "PAGE", checksum, 1) != deterministic_asset_key(7, "PAGE", checksum, 2)
    assert deterministic_asset_key(7, "PAGE", checksum, 1) != deterministic_asset_key(8, "PAGE", checksum, 1)


def test_derivative_is_reencoded_and_content_addressed() -> None:
    derivative = normalize_visual_derivative(_png(), "image/png")
    signal = validate_visual_bytes(derivative, "image/png")
    assert signal.content_type == "image/png"
    assert content_addressed_derivative_key(signal.checksum) == (
        f"objects/{signal.checksum[:2]}/{signal.checksum}"
    )
    assert derivative != _png()  # encoder normalization is deterministic, not source reuse


def test_derivative_strips_exif_and_polyglot_payloads_are_rejected() -> None:
    output = BytesIO()
    image = Image.new("RGB", (4, 3), (20, 40, 60))
    image.save(output, format="JPEG", exif=Image.Exif())
    source = output.getvalue()
    derivative = normalize_visual_derivative(source, "image/jpeg", output_format="JPEG")
    with Image.open(BytesIO(derivative)) as decoded:
        assert not decoded.getexif()
    with pytest.raises(VisualValidationError, match="trailing"):
        validate_visual_bytes(_png() + b"<script>bad</script>", "image/png")


def test_visual_validation_can_be_killed_at_worker_boundary() -> None:
    result = validate_visual_bytes_isolated(_png(), "image/png", timeout_seconds=5)
    assert (result.width, result.height) == (4, 3)


def test_derivative_normalization_has_isolated_timeout_and_output_bound() -> None:
    result = normalize_visual_derivative_isolated(_png(), "image/png", timeout_seconds=5)
    assert validate_visual_bytes(result, "image/png").width == 4
    with pytest.raises(VisualValidationError, match="output budget"):
        normalize_visual_derivative_isolated(_png(), "image/png", timeout_seconds=5, max_output_bytes=1)


def test_embedded_region_requires_reliable_lineage() -> None:
    lineage = embedded_region_lineage(document_id=7, version_id=3, page_number=2)
    assert lineage is not None
    assert (lineage.document_id, lineage.version_id, lineage.page_number) == (7, 3, 2)
    assert embedded_region_lineage(document_id=7, version_id=3, page_number=None) is None
    assert embedded_region_lineage(document_id=7, version_id=3, page_number=0) is None
    assert embedded_region_lineage(document_id=None, version_id=3, page_number=1) is None
    assert embedded_region_lineage(document_id=7, version_id=3, page_number=1, asset_type="UNKNOWN") is None
