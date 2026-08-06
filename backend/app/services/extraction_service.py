"""Hybrid text-extraction pipeline — FR-OCR-01 + FR-GL-01.

Tier 0 (synchronous, seconds):
  • Docling — structure-aware PDF/DOCX/image parser that preserves tables as
    Markdown and runs its own embedded OCR engine.  Falls back gracefully when
    not installed or when parsing fails.

  • Legacy path (fallback):
      native PDF with a text layer  -> pypdf (cheap CPU path)
      scanned PDF / image           -> Tesseract OCR via PyMuPDF rasterisation
      office / text files           -> python-docx / openpyxl / python-pptx

Everything degrades gracefully: if an optional library or the Tesseract
binary is missing, the document still ingests and its status reflects why.
"""

from __future__ import annotations

import io
import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TypeAlias

from ..config import settings
from ..observability import emit_event
from ..utils.request_context import bound_request_context, worker_context

# ── Optional dependencies (import-guarded) ────────────────────────────────────

# Docling — layout-aware, structure-preserving parser (Tier 0 preferred path).
try:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
        RapidOcrOptions,
    )
    from docling.document_converter import (
        DocumentConverter as _DoclingConverter,
    )
    from docling.document_converter import (
        PdfFormatOption,
    )

    _HAS_DOCLING = True
except Exception:  # pragma: no cover
    _HAS_DOCLING = False

# Lazy singleton — the converter is expensive to construct (loads ~770 model
# weights). We build it once on first use and cache it for the process lifetime
# so every subsequent upload reuses the already-loaded models.
_docling_converter: "_DoclingConverter | None" = None
_docling_converter_checked: bool = False
_docling_lock = None  # initialised below after threading is available


def _rapidocr_artifacts(repo_dir: Path) -> tuple[Path, Path, Path, Path | None]:
    """Return the downloaded RapidOCR model files under ``repo_dir``."""

    onnx_files = sorted(repo_dir.rglob("*.onnx"))

    def _tokens(path: Path) -> set[str]:
        # RapidOCR's downloader currently places files directly in the model
        # root (for example ``PP-OCRv6_det_small.onnx``), while older bundles
        # used ``.../det/...`` subdirectories.  Tokenize both path components
        # and filenames so both layouts are accepted.
        return set(re.findall(r"[a-z0-9]+", path.as_posix().lower()))

    def _find(kind: str) -> Path | None:
        return next(
            (path for path in onnx_files if kind in _tokens(path)),
            None,
        )

    det = _find("det")
    cls = _find("cls")
    rec = _find("rec")
    keys = next(
        (
            path
            for path in sorted(repo_dir.rglob("*.txt"))
            if {"keys", "dict"} & _tokens(path) or "rec" in _tokens(path)
        ),
        None,
    )
    if det is None or cls is None or rec is None:
        raise FileNotFoundError(
            "RapidOCR download did not produce detector, classifier, and recognizer models"
        )
    return det, cls, rec, keys


def _ensure_rapidocr_artifacts(repo_dir: Path) -> tuple[Path, Path, Path, Path | None]:
    """Download RapidOCR models into the persistent volume when missing."""

    repo_dir.mkdir(parents=True, exist_ok=True)
    try:
        artifacts = _rapidocr_artifacts(repo_dir)
        logging.getLogger(__name__).info(
            "rapidocr: using cached artifacts under %s", repo_dir
        )
        return artifacts
    except FileNotFoundError:
        pass

    try:
        import portalocker
        import rapidocr
        import yaml
        from rapidocr import download_models
    except ImportError as exc:  # pragma: no cover - depends on runtime extras
        raise RuntimeError("RapidOCR automatic downloader is unavailable") from exc

    lock_path = repo_dir.parent / ".rapidocr-download.lock"
    with portalocker.Lock(str(lock_path), timeout=900):
        try:
            artifacts = _rapidocr_artifacts(repo_dir)
            logging.getLogger(__name__).info(
                "rapidocr: another process populated cached artifacts under %s", repo_dir
            )
            return artifacts
        except FileNotFoundError:
            pass

        package_config = Path(rapidocr.__file__).with_name("config.yaml")
        config = yaml.safe_load(package_config.read_text(encoding="utf-8"))
        config.setdefault("Global", {})["model_root_dir"] = str(repo_dir)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", prefix="docvault-rapidocr-", delete=False
        ) as handle:
            yaml.safe_dump(config, handle, sort_keys=False)
            config_path = Path(handle.name)
        try:
            logging.getLogger(__name__).info(
                "rapidocr: downloading missing artifacts into %s", repo_dir
            )
            download_models(config_path)
        finally:
            config_path.unlink(missing_ok=True)

        return _rapidocr_artifacts(repo_dir)


def _get_docling_converter():
    """Return a process-level DocumentConverter singleton (thread-safe lazy init)."""
    global _docling_converter, _docling_converter_checked, _docling_lock
    if _docling_converter_checked:
        return _docling_converter
    import threading

    if _docling_lock is None:
        _docling_lock = threading.Lock()
    with _docling_lock:
        if _docling_converter_checked:  # double-check after acquiring lock
            return _docling_converter
        _docling_converter_checked = True
        if not _HAS_DOCLING:
            return None
        try:
            # Store all Docling model artifacts in the persistent writable volume
            # so they survive container restarts and are never downloaded into
            # the read-only /opt/venv package directory.
            _docling_artifacts_path = settings.storage_dir / "docvault-docling-artifacts"
            _docling_artifacts_path.mkdir(parents=True, exist_ok=True)

            # Use the onnxruntime backend — it is installed via docling[rapidocr]
            # in the ai extra and works on every deployment, including those
            # without the visual extra (which is the only place torch lives).
            #
            _repo_dir = _docling_artifacts_path / "RapidOcr"
            _det, _cls, _rec, _keys = _ensure_rapidocr_artifacts(_repo_dir)
            _ocr_opts = RapidOcrOptions(
                lang=["english"],
                backend="onnxruntime",
                det_model_path=str(_det),
                cls_model_path=str(_cls),
                rec_model_path=str(_rec),
                rec_keys_path=str(_keys) if _keys is not None else None,
            )

            _pdf_pipeline_options = PdfPipelineOptions(
                do_ocr=True,
                do_table_structure=True,
                # artifacts_path is used by Docling's own layout/table models
                # (model.safetensors) AND as the fallback for RapidOCR when
                # explicit paths are not yet available.
                artifacts_path=_docling_artifacts_path,
                ocr_options=_ocr_opts,
                accelerator_options=AcceleratorOptions(
                    device=AcceleratorDevice.CPU,
                ),
            )
            _docling_converter = _DoclingConverter(
                allowed_formats=[
                    InputFormat.PDF,
                    InputFormat.IMAGE,
                    InputFormat.DOCX,
                    InputFormat.PPTX,
                    InputFormat.XLSX,
                    InputFormat.HTML,
                    InputFormat.MD,
                    InputFormat.CSV,
                ],
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=_pdf_pipeline_options
                    )
                },
            )

            emit_event(
                "extraction.engine.initialized",
                level=logging.INFO,
                component="extraction",
                operation="docling",
            )
        except Exception as exc:
            logging.error(f"Docling initialization failed: {exc}", exc_info=True)
            emit_event(
                "extraction.engine.initialized",
                level=logging.ERROR,
                component="extraction",
                operation="docling",
                outcome="error",
                error=exc,
            )
            _docling_converter = None
    return _docling_converter


# Legacy Tesseract stack
try:
    import pytesseract
    from PIL import Image

    _HAS_TESSERACT_LIB = True
except Exception:  # pragma: no cover
    _HAS_TESSERACT_LIB = False

try:
    import fitz  # PyMuPDF

    _HAS_FITZ = True
except Exception:  # pragma: no cover
    _HAS_FITZ = False

try:
    from pypdf import PdfReader

    _HAS_PYPDF = True
except Exception:  # pragma: no cover
    _HAS_PYPDF = False

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
TEXT_EXTS = {
    ".txt",
    ".md",
    ".csv",
    ".log",
    ".json",
    ".html",
    ".htm",
    ".xml",
    ".yaml",
    ".yml",
}
# Threshold of extractable chars per page below which a PDF is deemed "scanned".
_NATIVE_TEXT_MIN_CHARS = 40
EXTRACTION_PIPELINE_VERSION = "hybrid-v2"

QualitySignal: TypeAlias = bool | float | int | str


class _OcrLanguageUnavailable(RuntimeError):
    """Configured OCR language packs are absent from the worker runtime."""


def _component_version(distribution: str, fallback: str) -> str:
    try:
        value = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        value = fallback
    normalized = re.sub(r"[^A-Za-z0-9_.+-]", "-", value).strip("-")
    return (normalized or fallback)[:40]


def _detect_language(text: str) -> str:
    latin = sum(
        1
        for character in text
        if ("LATIN" in unicodedata.name(character, "") and character.isalpha())
    )
    devanagari = sum(1 for character in text if "\u0900" <= character <= "\u097f")
    if latin and devanagari:
        return "eng+hin"
    if devanagari:
        return "hin"
    if latin:
        return "eng"
    return "und"


def _measure_quality(
    text: str,
    *,
    page_count: int,
    ocr_confidence: float | None,
) -> tuple[float, dict[str, QualitySignal]]:
    character_count = len(text)
    non_whitespace = sum(1 for character in text if not character.isspace())
    printable = sum(1 for character in text if character.isprintable())
    replacements = text.count("\ufffd")
    pages = max(page_count, 1)
    printable_ratio = printable / character_count if character_count else 0.0
    replacement_ratio = replacements / character_count if character_count else 0.0
    characters_per_page = non_whitespace / pages
    density_score = min(1.0, characters_per_page / 80.0)
    cleanliness_score = max(0.0, printable_ratio - (replacement_ratio * 4.0))
    if ocr_confidence is None:
        score = (0.6 * cleanliness_score) + (0.4 * density_score)
    else:
        score = (
            (0.6 * max(0.0, min(1.0, ocr_confidence)))
            + (0.25 * cleanliness_score)
            + (0.15 * density_score)
        )
    signals: dict[str, QualitySignal] = {
        "character_count": character_count,
        "non_whitespace_characters": non_whitespace,
        "characters_per_page": round(characters_per_page, 3),
        "printable_ratio": round(printable_ratio, 4),
        "replacement_character_ratio": round(replacement_ratio, 4),
        "ocr_confidence_measured": ocr_confidence is not None,
    }
    return round(max(0.0, min(1.0, score)), 3), signals


@dataclass
class OcrResult:
    text: str = ""
    status: str = "pending"  # native | ocr | unavailable | skipped | error
    confidence: float | None = None
    page_count: int = 0
    language: str = "und"
    notes: list[str] = field(default_factory=list)
    extractor_name: str = "none"
    extractor_version: str = EXTRACTION_PIPELINE_VERSION
    ocr_engine: str | None = None
    ocr_engine_version: str | None = None
    ocr_languages: list[str] = field(default_factory=list)
    missing_ocr_languages: list[str] = field(default_factory=list)
    quality_score: float | None = None
    quality_signals: dict[str, QualitySignal] = field(default_factory=dict)


def _finalize_result(result: OcrResult) -> OcrResult:
    result.text = result.text.replace("\x00", "")
    result.language = _detect_language(result.text)
    result.quality_score, measured = _measure_quality(
        result.text,
        page_count=result.page_count,
        ocr_confidence=result.confidence,
    )
    result.quality_signals = {
        **measured,
        **result.quality_signals,
        "language": result.language,
        "page_count": max(0, result.page_count),
        "requested_ocr_languages": "+".join(settings.ocr_languages),
        "effective_ocr_languages": "+".join(result.ocr_languages),
        "missing_ocr_languages": "+".join(result.missing_ocr_languages),
        "ocr_language_pack_complete": not result.missing_ocr_languages,
    }
    return result


# ── Docling (Tier 0) ──────────────────────────────────────────────────────────


def _docling_available() -> bool:
    return _HAS_DOCLING and settings.use_docling


def _extract_with_docling(path: Path) -> OcrResult:
    """Use the cached Docling converter to produce structure-aware Markdown."""
    res = OcrResult(
        extractor_name="docling",
        extractor_version=_component_version("docling", EXTRACTION_PIPELINE_VERSION),
    )
    converter = _get_docling_converter()
    if converter is None:
        res.status = "error"
        res.notes.append("Docling converter not available")
        return res
    try:
        doc = converter.convert(str(path))
        # Export as Markdown — tables become pipe-tables, headings are preserved.
        md = doc.document.export_to_markdown()
        res.text = md or ""
        _num_pages = getattr(doc.document, "num_pages", 0)
        res.page_count = (
            int(_num_pages()) if callable(_num_pages) else int(_num_pages or 0)
        ) or 1
        # Docling does not expose one calibrated document-level confidence.
        # Parser success is therefore recorded through provenance and measured
        # text-quality signals, never as a fabricated confidence of 1.0.
        res.status = "native"
        res.confidence = None
        res.notes.append("parsed by Docling (layout-aware)")
    except Exception as exc:  # pragma: no cover
        res.status = "error"
        res.notes.append(f"Docling failed: {exc}")
    return res


# ── Legacy Tesseract stack ────────────────────────────────────────────────────


def _tesseract_available() -> bool:
    if not _HAS_TESSERACT_LIB:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _effective_tesseract_languages(
    available: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    requested = list(settings.ocr_languages)
    if available is None:
        try:
            available = set(pytesseract.get_languages(config=""))
        except Exception:
            available = set()
    effective = [language for language in requested if language in available]
    missing = [language for language in requested if language not in available]
    return effective, missing


def _ocr_image_bytes(
    data: bytes,
) -> tuple[str, float | None, list[str], list[str]]:
    """Run Tesseract and report measured confidence and language provenance."""
    image = Image.open(io.BytesIO(data))
    languages, missing_languages = _effective_tesseract_languages()
    if not languages:
        raise _OcrLanguageUnavailable
    language_argument = "+".join(languages)
    text = pytesseract.image_to_string(image, lang=language_argument)
    conf = None
    try:
        d = pytesseract.image_to_data(
            image,
            lang=language_argument,
            output_type=pytesseract.Output.DICT,
        )
        vals = [
            int(c)
            for c in d.get("conf", [])
            if str(c).lstrip("-").isdigit() and int(c) >= 0
        ]
        if vals:
            conf = round(sum(vals) / len(vals) / 100.0, 3)
    except Exception:
        pass
    return text, conf, languages, missing_languages


def _extract_pdf_legacy(path: Path) -> OcrResult:
    res = OcrResult(
        extractor_name="pypdf",
        extractor_version=_component_version("pypdf", EXTRACTION_PIPELINE_VERSION),
    )

    # 1) Try native text layer first (cheap CPU path).
    native_text = ""
    pages = 0
    if _HAS_PYPDF:
        try:
            reader = PdfReader(str(path))
            pages = len(reader.pages)
            native_text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as exc:
            res.notes.append(f"pypdf failed: {exc}")

    res.page_count = pages
    # Heuristic: enough extractable text (scaled by page count) => digital PDF.
    if native_text.strip() and len(native_text.strip()) >= _NATIVE_TEXT_MIN_CHARS * max(
        pages, 1
    ):
        res.text = native_text
        res.status = "native"
        res.confidence = None
        return res

    # 2) Scanned PDF -> rasterize pages and OCR them.
    if not _tesseract_available():
        res.text = native_text
        res.status = "unavailable" if not native_text.strip() else "native"
        res.notes.append("Tesseract binary not available for scanned pages")
        return res
    if not _HAS_FITZ:
        res.text = native_text
        res.status = "unavailable" if not native_text.strip() else "native"
        res.notes.append("PyMuPDF not installed; cannot rasterize scanned PDF")
        return res

    try:
        doc = fitz.open(str(path))
        res.page_count = doc.page_count
        chunks, confs = [], []
        effective_languages: set[str] = set()
        missing_languages: set[str] = set()
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            text, conf, languages, missing = _ocr_image_bytes(pix.tobytes("png"))
            chunks.append(text)
            if conf is not None:
                confs.append(conf)
            effective_languages.update(languages)
            missing_languages.update(missing)
        res.text = "\n".join(chunks)
        res.status = "ocr"
        res.confidence = round(sum(confs) / len(confs), 3) if confs else None
        res.extractor_name = "pymupdf+tesseract"
        res.extractor_version = _component_version(
            "PyMuPDF",
            EXTRACTION_PIPELINE_VERSION,
        )
        res.ocr_engine = "tesseract"
        res.ocr_engine_version = str(pytesseract.get_tesseract_version())[:40]
        res.ocr_languages = sorted(effective_languages)
        res.missing_ocr_languages = sorted(missing_languages)
        if res.missing_ocr_languages:
            res.notes.append("one or more configured OCR language packs are unavailable")
    except _OcrLanguageUnavailable:
        res.text = native_text
        res.status = "unavailable"
        res.missing_ocr_languages = list(settings.ocr_languages)
        res.notes.append("configured OCR language packs are unavailable")
    except Exception as exc:
        res.text = native_text
        res.status = "error"
        res.notes.append(f"OCR failed: {exc}")
    return res


def _extract_image_legacy(path: Path) -> OcrResult:
    res = OcrResult(
        page_count=1,
        extractor_name="pillow+tesseract",
        extractor_version=_component_version("Pillow", EXTRACTION_PIPELINE_VERSION),
    )
    if not _tesseract_available():
        res.status = "unavailable"
        res.notes.append("Tesseract binary not available")
        return res
    try:
        text, conf, languages, missing = _ocr_image_bytes(path.read_bytes())
        res.text, res.confidence, res.status = text, conf, "ocr"
        res.ocr_engine = "tesseract"
        res.ocr_engine_version = str(pytesseract.get_tesseract_version())[:40]
        res.ocr_languages = languages
        res.missing_ocr_languages = missing
        if missing:
            res.notes.append("one or more configured OCR language packs are unavailable")
    except _OcrLanguageUnavailable:
        res.status = "unavailable"
        res.missing_ocr_languages = list(settings.ocr_languages)
        res.notes.append("configured OCR language packs are unavailable")
    except Exception as exc:
        res.status = "error"
        res.notes.append(f"OCR failed: {exc}")
    return res


def _extract_office_or_text(path: Path, extension: str | None = None) -> OcrResult:
    res = OcrResult(page_count=1, confidence=None)
    ext = (extension or path.suffix).lower()
    try:
        if ext in TEXT_EXTS:
            res.text = path.read_text(errors="ignore")
            res.extractor_name = "plain-text"
            res.extractor_version = EXTRACTION_PIPELINE_VERSION
        elif ext == ".docx":
            import docx  # python-docx

            res.extractor_name = "python-docx"
            res.extractor_version = _component_version(
                "python-docx",
                EXTRACTION_PIPELINE_VERSION,
            )
            doc = docx.Document(str(path))
            parts = []
            for para in doc.paragraphs:
                if para.text.strip():
                    parts.append(para.text)
            for table in doc.tables:
                # Render tables as Markdown for better structure preservation.
                table_rows = [
                    [cell.text.strip() for cell in row.cells] for row in table.rows
                ]
                if table_rows:
                    header = "| " + " | ".join(table_rows[0]) + " |"
                    separator = "| " + " | ".join(["---"] * len(table_rows[0])) + " |"
                    body = "\n".join(
                        "| " + " | ".join(row) + " |" for row in table_rows[1:]
                    )
                    parts.append("\n".join([header, separator, body]))
            res.text = "\n\n".join(parts)
        elif ext == ".xlsx":
            import openpyxl

            res.extractor_name = "openpyxl"
            res.extractor_version = _component_version(
                "openpyxl",
                EXTRACTION_PIPELINE_VERSION,
            )
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            sheet_lines: list[str] = []
            for ws in wb.worksheets:
                sheet_lines.append(f"## Sheet: {ws.title}")
                sheet_rows = list(ws.iter_rows(values_only=True))
                if sheet_rows:
                    # Render as Markdown table.
                    header = sheet_rows[0]
                    sheet_lines.append(
                        "| "
                        + " | ".join(
                            str(cell) if cell is not None else "" for cell in header
                        )
                        + " |"
                    )
                    sheet_lines.append("| " + " | ".join(["---"] * len(header)) + " |")
                    for row in sheet_rows[1:]:
                        sheet_lines.append(
                            "| "
                            + " | ".join(
                                "" if cell is None else str(cell) for cell in row
                            )
                            + " |"
                        )
            res.text = "\n".join(sheet_lines)
        elif ext == ".pptx":
            from pptx import Presentation

            res.extractor_name = "python-pptx"
            res.extractor_version = _component_version(
                "python-pptx",
                EXTRACTION_PIPELINE_VERSION,
            )
            prs = Presentation(str(path))
            chunks, slides = [], 0
            for slide in prs.slides:
                slides += 1
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        chunks.append(shape.text_frame.text)
                    if shape.has_table:
                        for r in shape.table.rows:
                            chunks.append(" | ".join(c.text for c in r.cells))
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                    chunks.append(slide.notes_slide.notes_text_frame.text)
            res.text = "\n".join(c for c in chunks if c and c.strip())
            res.page_count = slides
        else:
            res.status = "skipped"
            res.notes.append(
                f"Stored, but no text extractor for {ext} (still searchable by title)"
            )
            return res
        res.status = "native"
    except Exception as exc:
        res.status = "error"
        res.notes.append(f"text extraction failed: {exc}")
    return res


# ── Public API ─────────────────────────────────────────────────────────────────


def extract_text(path: Path, content_type: str = "", filename: str = "") -> OcrResult:
    """Route a stored file to the right extractor and return text + status.

    Tier 0 (preferred): Docling — structure-aware, table-preserving parser.
    Fallback: legacy Tesseract / pypdf / office parsers.
    """
    ext = Path(filename).suffix.lower() if filename else path.suffix.lower()

    # Cheap preflight budgets protect optional parsers and OCR from hostile
    # page/pixel/text expansion.  The file has already passed signature checks.
    if ext == ".pdf" and _HAS_PYPDF:
        try:
            if len(PdfReader(str(path)).pages) > settings.max_document_pages:
                return _finalize_result(
                    OcrResult(
                        status="skipped",
                        extractor_name="preflight",
                        notes=["page budget exceeded"],
                    )
                )
        except Exception:
            pass
    if ext in IMAGE_EXTS and _HAS_TESSERACT_LIB:
        try:
            with Image.open(path) as image:
                if image.width * image.height > settings.max_image_pixels:
                    return _finalize_result(
                        OcrResult(
                            status="skipped",
                            page_count=1,
                            extractor_name="preflight",
                            notes=["pixel budget exceeded"],
                        )
                    )
        except Exception:
            pass

    # Docling handles PDF, DOCX, PPTX, XLSX, HTML, Markdown, CSV, and image
    # formats in one unified pipeline.
    if _docling_available() and ext in {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".html",
        ".htm",
        ".md",
        ".csv",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
    }:
        result = _extract_with_docling(path)
        # Only fall back if Docling actually failed (no text extracted + error).
        if result.text and result.status != "error":
            return _finalize_result(result)
        # Docling failed — record the note and fall through to the legacy path.
        result.notes.append("falling back to legacy extractor")

    # Legacy path.
    if ext == ".pdf":
        result = _extract_pdf_legacy(path)
    elif ext in IMAGE_EXTS:
        result = _extract_image_legacy(path)
    else:
        result = _extract_office_or_text(path, ext)
    if len(result.text) > settings.max_extracted_text_chars:
        result.text = result.text[: settings.max_extracted_text_chars]
        result.notes.append("text budget exceeded; output truncated")
        result.quality_signals["text_truncated"] = True
    return _finalize_result(result)


def engine_status() -> dict:
    effective_languages, missing_languages = (
        _effective_tesseract_languages() if _HAS_TESSERACT_LIB else ([], list(settings.ocr_languages))
    )
    return {
        "docling": _docling_available(),
        "tesseract": _tesseract_available(),
        "pymupdf": _HAS_FITZ,
        "pypdf": _HAS_PYPDF,
        "ocr_languages": effective_languages,
        "missing_ocr_languages": missing_languages,
    }


def passive_engine_status() -> dict:
    """Report imported capabilities without invoking binaries or loading models."""

    return {
        "docling": _docling_available(),
        "tesseract": _HAS_TESSERACT_LIB,
        "pymupdf": _HAS_FITZ,
        "pypdf": _HAS_PYPDF,
        "requested_ocr_languages": list(settings.ocr_languages),
    }


def warm_docling() -> None:
    """Pre-load the Docling DocumentConverter in a background thread.

    Call this from the FastAPI startup event so the first uploaded file never
    triggers the expensive 770-weight cold-start. The converter is cached as a
    module-level singleton and reused for every subsequent request.
    """
    import threading

    context = worker_context("docling-warm")

    def _load() -> None:
        with bound_request_context(context):
            if not _docling_available():
                emit_event(
                    "worker.model_warm.completed",
                    context=context,
                    component="extraction",
                    operation="docling",
                    outcome="disabled",
                )
                return
            emit_event(
                "worker.model_warm.started",
                context=context,
                component="extraction",
                operation="docling",
            )
            conv = _get_docling_converter()
            emit_event(
                "worker.model_warm.completed",
                context=context,
                component="extraction",
                operation="docling",
                outcome="success" if conv is not None else "unavailable",
            )

    t = threading.Thread(target=_load, name="docling-warm", daemon=True)
    try:
        t.start()
    except Exception as exc:
        emit_event(
            "worker.model_warm.rejected",
            level=logging.ERROR,
            context=context,
            component="extraction",
            operation="thread_start",
            outcome="error",
            error=exc,
        )
        raise
