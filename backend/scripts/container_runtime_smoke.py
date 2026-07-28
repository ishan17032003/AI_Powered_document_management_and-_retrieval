"""Exercise document capabilities inside a built production container image.

Run this file through a read-only bind mount; it creates all fixtures in memory
and never opens the configured DocVault database or storage directory.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
from pathlib import Path

import cv2
import fitz
import numpy as np
import pytesseract
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

EXPECTED_PROVIDER_LIMITS = {
    "DOCVAULT_RAG_PROVIDER_CONNECT_TIMEOUT_SECONDS": "3",
    "DOCVAULT_RAG_PROVIDER_READ_TIMEOUT_SECONDS": "20",
    "DOCVAULT_RAG_PROVIDER_TOTAL_TIMEOUT_SECONDS": "30",
    "DOCVAULT_RAG_PROVIDER_MAX_OUTPUT_TOKENS": "512",
    "DOCVAULT_RAG_MAX_CONTEXT_BYTES": "32768",
    "DOCVAULT_RAG_PROVIDER_MAX_CONCURRENCY": "2",
}


def verify_system_surface() -> dict[str, object]:
    assert os.getuid() == 10001
    assert os.getgid() == 10001
    assert shutil.which("tesseract") == "/usr/bin/tesseract"
    assert shutil.which("curl") is None
    for unused_pdf_command in ("pdftotext", "pdfinfo", "pdftoppm", "pdftocairo"):
        assert shutil.which(unused_pdf_command) is None

    assert Path("/app/alembic.ini").is_file()
    migration_files = sorted(Path("/app/migrations/versions").glob("*.py"))
    assert migration_files
    assert Path("/entrypoint.sh").is_file()
    assert os.access("/entrypoint.sh", os.X_OK)
    assert importlib.util.find_spec("docling") is not None

    for field, expected in EXPECTED_PROVIDER_LIMITS.items():
        assert os.environ[field] == expected

    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "migration_files": len(migration_files),
        "tesseract_version": str(pytesseract.get_tesseract_version()),
        "docling_importable": True,
    }


def verify_pdf_capabilities() -> dict[str, object]:
    source = fitz.open()
    page = source.new_page()
    page.insert_text((72, 72), "DocVault PDF capability 2026", fontsize=18)
    pdf_bytes = source.tobytes()
    source.close()

    rendered = fitz.open(stream=pdf_bytes, filetype="pdf")
    extracted_text = rendered[0].get_text()
    pixmap = rendered[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    rendered.close()

    pypdf_text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert "DocVault PDF capability 2026" in extracted_text
    assert "DocVault PDF capability 2026" in pypdf_text
    assert pixmap.width > 0 and pixmap.height > 0

    return {
        "pdf_bytes": len(pdf_bytes),
        "rendered_width": pixmap.width,
        "rendered_height": pixmap.height,
        "pymupdf_text": True,
        "pypdf_text": True,
    }


def verify_image_and_ocr_capabilities() -> dict[str, object]:
    image = Image.new("RGB", (1200, 240), "white")
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        64,
    )
    ImageDraw.Draw(image).text(
        (40, 70),
        "DOCVAULT OCR 2026",
        fill="black",
        font=font,
    )

    png_buffer = io.BytesIO()
    image.save(png_buffer, format="PNG")
    png_bytes = png_buffer.getvalue()

    pillow_decoded = Image.open(io.BytesIO(png_bytes))
    pillow_decoded.load()
    opencv_decoded = cv2.imdecode(
        np.frombuffer(png_bytes, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    ocr_text = pytesseract.image_to_string(
        pillow_decoded,
        config="--psm 7",
        lang="eng",
    )

    assert pillow_decoded.size == (1200, 240)
    assert opencv_decoded is not None
    assert tuple(opencv_decoded.shape[:2]) == (240, 1200)
    assert "DOCVAULT" in ocr_text.upper()
    assert "2026" in ocr_text

    return {
        "png_bytes": len(png_bytes),
        "pillow_decoded": True,
        "opencv_decoded": True,
        "ocr_text": ocr_text.strip(),
    }


def main() -> int:
    result = {
        "system": verify_system_surface(),
        "pdf": verify_pdf_capabilities(),
        "image_ocr": verify_image_and_ocr_capabilities(),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
