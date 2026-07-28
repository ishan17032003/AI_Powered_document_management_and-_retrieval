from pathlib import Path

import pytest

from app.services.page_renderer import render_document_pages


def test_renderer_rejects_unbounded_settings(tmp_path: Path):
    with pytest.raises(ValueError):
        render_document_pages(tmp_path / "missing.pdf", dpi=10)
    with pytest.raises(ValueError):
        render_document_pages(tmp_path / "missing.pdf", max_pages=0)


def test_renderer_pins_page_count_and_checksum(tmp_path: Path):
    fitz = pytest.importorskip("fitz")
    source = tmp_path / "fixture.pdf"
    document = fitz.open()
    document.new_page(width=200, height=100)
    document.save(source)
    document.close()

    first = render_document_pages(source, dpi=72)
    second = render_document_pages(source, dpi=72)
    assert len(first) == len(second) == 1
    assert first[0].checksum == second[0].checksum
    assert first[0].renderer_version == "pymupdf-rgb-v1"
    assert first[0].page_number == 1
