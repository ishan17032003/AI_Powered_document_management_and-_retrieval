"""Pinned, deterministic document-page rendering hooks (MM-010)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class PageRenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RenderedPage:
    page_number: int
    width: int
    height: int
    checksum: str
    renderer_version: str
    dpi: int
    pixels: bytes


RENDERER_VERSION = "pymupdf-rgb-v1"


def render_document_pages(
    path: str | Path,
    *,
    dpi: int = 144,
    max_pages: int = 100,
) -> list[RenderedPage]:
    """Render pages with pinned DPI/colorspace and bounded resource use."""
    if type(dpi) is not int or not 36 <= dpi <= 600:
        raise ValueError("dpi must be between 36 and 600")
    if type(max_pages) is not int or not 1 <= max_pages <= 10_000:
        raise ValueError("max_pages must be between 1 and 10000")
    try:
        import fitz

        document = fitz.open(str(path))
    except Exception as exc:
        raise PageRenderError("pinned page renderer unavailable") from exc
    try:
        if document.page_count > max_pages:
            raise PageRenderError("page render budget exceeded")
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pages: list[RenderedPage] = []
        for number in range(document.page_count):
            pixmap = document.load_page(number).get_pixmap(
                matrix=matrix, colorspace=fitz.csRGB, alpha=False
            )
            pixels = bytes(pixmap.samples)
            pages.append(
                RenderedPage(
                    page_number=number + 1,
                    width=pixmap.width,
                    height=pixmap.height,
                    checksum=hashlib.sha256(pixels).hexdigest(),
                    renderer_version=RENDERER_VERSION,
                    dpi=dpi,
                    pixels=pixels,
                )
            )
        return pages
    finally:
        document.close()


def render_document_pages_bytes(
    data: bytes,
    *,
    filetype: str = "pdf",
    dpi: int = 144,
    max_pages: int = 100,
) -> list[RenderedPage]:
    """Render an already-quarantined PDF without exposing a server path.

    The byte-oriented entry point keeps visual processing behind the object-store
    boundary and mirrors the same pinned RGB/resource limits as the path API.
    """

    if not isinstance(data, bytes) or not data:
        raise PageRenderError("page payload is empty")
    if type(dpi) is not int or not 36 <= dpi <= 600:
        raise ValueError("dpi must be between 36 and 600")
    if type(max_pages) is not int or not 1 <= max_pages <= 10_000:
        raise ValueError("max_pages must be between 1 and 10000")
    try:
        import fitz

        document = fitz.open(stream=data, filetype=filetype)
    except Exception as exc:
        raise PageRenderError("pinned page renderer unavailable") from exc
    try:
        if document.page_count > max_pages:
            raise PageRenderError("page render budget exceeded")
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pages: list[RenderedPage] = []
        for number in range(document.page_count):
            pixmap = document.load_page(number).get_pixmap(
                matrix=matrix, colorspace=fitz.csRGB, alpha=False
            )
            pixels = bytes(pixmap.samples)
            pages.append(
                RenderedPage(
                    page_number=number + 1,
                    width=pixmap.width,
                    height=pixmap.height,
                    checksum=hashlib.sha256(pixels).hexdigest(),
                    renderer_version=RENDERER_VERSION,
                    dpi=dpi,
                    pixels=pixels,
                )
            )
        return pages
    finally:
        document.close()
