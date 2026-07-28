"""Backward-compatible imports for the extraction service.

New application code should import :mod:`app.services.extraction_service`.
"""

from .services.extraction_service import (
    IMAGE_EXTS,
    TEXT_EXTS,
    OcrResult,
    engine_status,
    extract_text,
    warm_docling,
)

__all__ = [
    "IMAGE_EXTS",
    "TEXT_EXTS",
    "OcrResult",
    "engine_status",
    "extract_text",
    "warm_docling",
]
