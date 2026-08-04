"""Build searchable visual derivatives during ingestion.

The service always persists safe image/page derivatives and their authoritative
OCR text as untrusted retrieval context.  When the optional local SigLIP2 stage
is enabled, the same assets are embedded into a model-revisioned LanceDB lane
without changing asset lineage or the API contract.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..storage import object_store
from . import page_renderer, visual_assets, visual_retrieval, visual_semantic_service

VISUAL_MANIFEST_VERSION = "visual-text-v2"
VISUAL_STAGE_NAME = "VISUAL_INDEXING"

# Vector figure detection: charts/diagrams drawn as PDF vector graphics leave
# no embedded raster to extract, so dense drawing clusters are rendered as
# REGION assets instead. Bounds keep decorative rules/underlines (few
# primitives, extreme aspect) and full-page layouts out of the figure set.
_FIGURE_MERGE_MARGIN = 12.0
_FIGURE_MIN_PRIMITIVES = 8
_FIGURE_MIN_AREA_FRACTION = 0.03
_FIGURE_MAX_AREA_FRACTION = 0.75
_FIGURE_MAX_ASPECT = 12.0
_FIGURE_MAX_PER_PAGE = 4
_FIGURE_RENDER_ZOOM = 2.0


def _figure_regions(page) -> list:
    """Cluster vector drawings into bounded figure rectangles for one page."""

    import fitz

    rects = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing["rect"])
        if rect.is_empty or rect.is_infinite:
            continue
        rects.append(rect)
    if not rects:
        return []

    clusters: list[list] = []  # [bounding Rect, primitive count]
    for rect in rects:
        expanded = rect + (
            -_FIGURE_MERGE_MARGIN,
            -_FIGURE_MERGE_MARGIN,
            _FIGURE_MERGE_MARGIN,
            _FIGURE_MERGE_MARGIN,
        )
        merged = None
        for cluster in clusters:
            if cluster[0].intersects(expanded):
                cluster[0].include_rect(rect)
                cluster[1] += 1
                merged = cluster
                break
        if merged is None:
            clusters.append([fitz.Rect(rect), 1])
            continue
        # Growing a cluster can bridge it into a neighbour; re-merge to a
        # fixed point so overlapping figures never produce duplicate regions.
        changed = True
        while changed:
            changed = False
            for index, cluster in enumerate(clusters):
                for other in clusters[index + 1 :]:
                    if cluster[0].intersects(other[0]):
                        cluster[0].include_rect(other[0])
                        cluster[1] += other[1]
                        clusters.remove(other)
                        changed = True
                        break
                if changed:
                    break

    page_area = abs(page.rect.get_area()) or 1.0
    regions = []
    for rect, primitives in clusters:
        area_fraction = abs(rect.get_area()) / page_area
        width = rect.width or 0.001
        height = rect.height or 0.001
        aspect = max(width / height, height / width)
        if (
            primitives >= _FIGURE_MIN_PRIMITIVES
            and _FIGURE_MIN_AREA_FRACTION <= area_fraction <= _FIGURE_MAX_AREA_FRACTION
            and aspect <= _FIGURE_MAX_ASPECT
        ):
            regions.append(rect)
    regions.sort(key=lambda value: -abs(value.get_area()))
    return regions[:_FIGURE_MAX_PER_PAGE]


@dataclass(frozen=True, slots=True)
class VisualProcessingResult:
    state: str
    asset_count: int = 0
    extraction_count: int = 0
    mode: str = "disabled"
    semantic_state: str = "disabled"
    semantic_image_count: int = 0
    semantic_page_count: int = 0
    semantic_error_code: str | None = None


def _persist_derivative(payload: bytes, *, suffix: str = ".png") -> str:
    staged = object_store.stage(BytesIO(payload), suffix=suffix)
    try:
        return object_store.promote(staged.key, checksum=staged.checksum)
    except Exception:
        staged.path.unlink(missing_ok=True)
        raise


def _page_texts(data: bytes, page_count: int, fallback: str) -> list[str]:
    """Read native PDF page text and fall back conservatively to OCR text."""

    try:
        import fitz

        document = fitz.open(stream=data, filetype="pdf")
        try:
            values = [document.load_page(index).get_text("text").strip() for index in range(document.page_count)]
        finally:
            document.close()
    except Exception:
        values = []
    if len(values) != page_count:
        values = [""] * page_count
    if any(values):
        return values
    if not fallback.strip():
        return values
    # Legacy OCR currently stores one document-level string.  Keep it on the
    # first page rather than inventing text/page boundaries for every page.
    return [fallback.strip()] + [""] * max(0, page_count - 1)


def _register_extraction(
    db: Session,
    *,
    asset: models.VisualAsset,
    version: models.DocVersion,
    document: models.Document,
    text: str,
    page_number: int | None,
) -> bool:
    if not text.strip():
        return False
    visual_retrieval.persist_visual_output(
        db,
        asset_id=asset.id,
        version_id=version.id,
        output_type="OCR",
        text=text,
        engine_revision=f"{version.extractor_version or 'unknown'}:visual-text-v1",
        confidence=version.extraction_quality_score,
        language=document.language,
        quality_signals={
            "source": "document_ocr",
            "page_number": page_number,
            "trusted": False,
        },
    )
    return True


def _process_image(
    db: Session,
    *,
    document: models.Document,
    version: models.DocVersion,
    data: bytes,
) -> tuple[int, int]:
    content_type = version.content_type.split(";", 1)[0].strip().lower()
    normalized = visual_assets.normalize_visual_derivative_isolated(
        data,
        content_type,
        output_format="PNG",
        max_output_bytes=settings.max_upload_bytes,
    )
    file_key = _persist_derivative(normalized)
    asset = visual_assets.register_asset(
        db,
        document_id=document.id,
        version_id=version.id,
        asset_type="IMAGE",
        file_key=file_key,
        content_type="image/png",
        payload=normalized,
        page_number=1,
    )
    extracted = int(
        _register_extraction(
            db,
            asset=asset,
            version=version,
            document=document,
            text=version.ocr_text or document.title,
            page_number=1,
        )
    )
    return 1, extracted


def _markdown_embedded_images(text: str) -> list[tuple[str, str, bytes]]:
    """Return (alt, subtype, payload) for each base64 data-URI image.

    Markdown references external images by URL; those are never fetched (no
    egress) and relative paths do not resolve inside the vault, so data URIs
    are the only image bytes the file itself carries.  The installed CommonMark
    parser (markdown-it-py, a Docling dependency) locates image nodes
    structurally — the parser-function analogue of fitz for PDFs.
    """
    from markdown_it import MarkdownIt

    results: list[tuple[str, str, bytes]] = []
    for token in MarkdownIt("commonmark").parse(text):
        for child in token.children or ():
            if child.type != "image":
                continue
            src = str(child.attrGet("src") or "")
            header, separator, encoded = src.partition(";base64,")
            if not separator or not header.startswith("data:image/"):
                continue
            subtype = header[len("data:image/") :].lower()
            if subtype not in {"png", "jpeg", "jpg", "gif", "webp"}:
                continue
            try:
                payload = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if payload:
                results.append((child.content or "", subtype, payload))
    return results


def _process_markdown(
    db: Session,
    *,
    document: models.Document,
    version: models.DocVersion,
    data: bytes,
) -> tuple[int, int]:
    """Register images a Markdown file embeds as base64 data URIs."""
    assets = 0
    extractions = 0
    for alt, subtype, payload in _markdown_embedded_images(
        data.decode("utf-8", errors="ignore")
    ):
        if assets >= settings.visual_max_assets_per_version:
            break
        try:
            normalized = visual_assets.normalize_visual_derivative_isolated(
                payload,
                f"image/{'jpeg' if subtype == 'jpg' else subtype}",
                output_format="PNG",
                max_output_bytes=settings.max_upload_bytes,
            )
        except Exception:
            # A corrupt or oversized embed must not fail the whole stage.
            continue
        file_key = _persist_derivative(normalized)
        asset = visual_assets.register_asset(
            db,
            document_id=document.id,
            version_id=version.id,
            asset_type="IMAGE",
            file_key=file_key,
            content_type="image/png",
            payload=normalized,
            page_number=1,
        )
        assets += 1
        extractions += int(
            _register_extraction(
                db,
                asset=asset,
                version=version,
                document=document,
                text=alt.strip() or document.title,
                page_number=1,
            )
        )
    return assets, extractions


def _process_pdf(
    db: Session,
    *,
    document: models.Document,
    version: models.DocVersion,
    data: bytes,
) -> tuple[int, int]:
    pages = page_renderer.render_document_pages_bytes(
        data,
        dpi=144,
        max_pages=min(
            settings.max_document_pages,
            500,
            settings.visual_max_assets_per_version,
        ),
    )
    texts = _page_texts(data, len(pages), version.ocr_text)
    assets = 0
    extractions = 0
    page_assets: dict[int, models.VisualAsset] = {}
    from PIL import Image

    for page, text in zip(pages, texts, strict=True):
        if assets >= settings.visual_max_assets_per_version:
            break
        output = BytesIO()
        Image.frombytes("RGB", (page.width, page.height), page.pixels).save(
            output,
            format="PNG",
            optimize=True,
        )
        payload = output.getvalue()
        file_key = _persist_derivative(payload)
        asset = visual_assets.register_asset(
            db,
            document_id=document.id,
            version_id=version.id,
            asset_type="PAGE",
            file_key=file_key,
            content_type="image/png",
            payload=payload,
            page_number=page.page_number,
        )
        page_assets[page.page_number] = asset
        assets += 1
        extractions += int(
            _register_extraction(
                db,
                asset=asset,
                version=version,
                document=document,
                text=text,
                page_number=page.page_number,
            )
        )

    # A rendered page is a distinct searchable visual, but figures and scans
    # embedded inside a PDF are also first-class image assets.  Keep extraction
    # bounded per version and attach the authoritative page text as context;
    # later caption/model stages can add richer untrusted text to the same
    # lineage without changing the API contract.
    try:
        import fitz

        document_handle = fitz.open(stream=data, filetype="pdf")
        try:
            seen: set[tuple[int, int]] = set()
            for page_number in range(document_handle.page_count):
                if assets >= settings.visual_max_assets_per_version:
                    break
                page = document_handle.load_page(page_number)
                page_text = texts[page_number] if page_number < len(texts) else ""
                for image_info in page.get_images(full=True):
                    if assets >= settings.visual_max_assets_per_version:
                        break
                    xref = int(image_info[0]) if image_info else 0
                    if xref <= 0 or (page_number, xref) in seen:
                        continue
                    seen.add((page_number, xref))
                    try:
                        extracted = document_handle.extract_image(xref)
                        payload = extracted.get("image", b"")
                        extension = str(extracted.get("ext", "")).lower()
                        content_type = {
                            "png": "image/png",
                            "jpg": "image/jpeg",
                            "jpeg": "image/jpeg",
                            "gif": "image/gif",
                            "webp": "image/webp",
                            "tif": "image/tiff",
                            "tiff": "image/tiff",
                        }.get(extension)
                        if not isinstance(payload, bytes) or not content_type:
                            continue
                        if len(payload) > settings.max_upload_bytes:
                            continue
                        normalized = visual_assets.normalize_visual_derivative_isolated(
                            payload,
                            content_type,
                            output_format="PNG",
                            max_output_bytes=settings.max_upload_bytes,
                        )
                        file_key = _persist_derivative(normalized)
                        asset = visual_assets.register_asset(
                            db,
                            document_id=document.id,
                            version_id=version.id,
                            asset_type="IMAGE",
                            file_key=file_key,
                            content_type="image/png",
                            payload=normalized,
                            page_number=page_number + 1,
                            source_asset=page_assets.get(page_number + 1),
                            relationship_type="REGION_OF",
                        )
                        assets += 1
                        extractions += int(
                            _register_extraction(
                                db,
                                asset=asset,
                                version=version,
                                document=document,
                                text=page_text or document.title,
                                page_number=page_number + 1,
                            )
                        )
                    except SQLAlchemyError:
                        raise
                    except Exception:
                        # One malformed/unsupported embedded object must not
                        # discard otherwise valid page assets.  The page-level
                        # visual stage remains observable through its manifest.
                        continue
                for region in _figure_regions(page):
                    if assets >= settings.visual_max_assets_per_version:
                        break
                    try:
                        pixmap = page.get_pixmap(
                            matrix=fitz.Matrix(
                                _FIGURE_RENDER_ZOOM, _FIGURE_RENDER_ZOOM
                            ),
                            clip=region,
                        )
                        payload = pixmap.tobytes("png")
                        if len(payload) > settings.max_upload_bytes:
                            continue
                        normalized = visual_assets.normalize_visual_derivative_isolated(
                            payload,
                            "image/png",
                            output_format="PNG",
                            max_output_bytes=settings.max_upload_bytes,
                        )
                        file_key = _persist_derivative(normalized)
                        asset = visual_assets.register_asset(
                            db,
                            document_id=document.id,
                            version_id=version.id,
                            asset_type="REGION",
                            file_key=file_key,
                            content_type="image/png",
                            payload=normalized,
                            page_number=page_number + 1,
                            source_asset=page_assets.get(page_number + 1),
                            relationship_type="REGION_OF",
                        )
                        assets += 1
                        extractions += int(
                            _register_extraction(
                                db,
                                asset=asset,
                                version=version,
                                document=document,
                                text=page_text or document.title,
                                page_number=page_number + 1,
                            )
                        )
                    except SQLAlchemyError:
                        raise
                    except Exception:
                        # A single unrenderable region must not discard the
                        # page assets or the remaining figures.
                        continue
        finally:
            document_handle.close()
    except SQLAlchemyError:
        raise
    except Exception:
        # PDF render/page text already completed; embedded image discovery is
        # additive and may be unavailable for a particular container.
        pass
    return assets, extractions


def process_version_visuals(
    db: Session,
    *,
    document: models.Document,
    version: models.DocVersion,
) -> VisualProcessingResult:
    """Create idempotent searchable image/page assets for supported files."""

    if not settings.visual_search_enabled:
        return VisualProcessingResult("disabled", mode="visual_search_disabled")

    content_type = version.content_type.split(";", 1)[0].strip().lower()
    # Browsers declare .md inconsistently (text/markdown, text/plain, empty),
    # so the filename decides alongside the content type.
    is_markdown = content_type in {"text/markdown", "text/x-markdown"} or (
        version.filename or ""
    ).lower().endswith(".md")
    if not (
        content_type.startswith("image/")
        or content_type == "application/pdf"
        or is_markdown
    ):
        return VisualProcessingResult("disabled", mode="unsupported_source")

    manifest = visual_assets.ensure_manifest(
        db,
        version.id,
        VISUAL_MANIFEST_VERSION,
        version.extractor_version or "visual-text-v1",
    )
    if manifest.state == "READY":
        semantic = visual_semantic_service.index_version(
            db,
            document=document,
            version=version,
        )
        return VisualProcessingResult(
            "ready",
            asset_count=manifest.derivative_count,
            mode=("visual_text+siglip2" if semantic.state == "ready" else "visual_text"),
            semantic_state=semantic.state,
            semantic_image_count=semantic.image_count,
            semantic_page_count=semantic.page_count,
            semantic_error_code=semantic.error_code,
        )
    visual_assets.transition_manifest(
        db,
        manifest,
        stage="VALIDATE",
        state="RUNNING",
    )
    try:
        with object_store.open(version.file_key) as handle:
            data = handle.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise ValueError("visual source exceeds upload budget")

        visual_assets.transition_manifest(db, manifest, stage="DERIVE", state="RUNNING")
        if content_type.startswith("image/"):
            assets, extractions = _process_image(
                db,
                document=document,
                version=version,
                data=data,
            )
        elif is_markdown:
            assets, extractions = _process_markdown(
                db,
                document=document,
                version=version,
                data=data,
            )
        else:
            assets, extractions = _process_pdf(
                db,
                document=document,
                version=version,
                data=data,
            )
        semantic = visual_semantic_service.index_version(
            db,
            document=document,
            version=version,
        )
        visual_assets.transition_manifest(db, manifest, stage="PERSIST", state="RUNNING")
        manifest.derivative_count = assets
        visual_assets.transition_manifest(db, manifest, stage="PERSIST", state="READY")
    except Exception as exc:
        # Keep failures durable and retryable.  The ingestion worker records the
        # optional stage as DEGRADED/REVIEW, while the manifest tells operators
        # exactly which visual run must be retried.
        try:
            visual_assets.transition_manifest(
                db,
                manifest,
                stage=manifest.stage or "DERIVE",
                state="FAILED",
                error_code="VISUAL_PROCESSING_FAILED",
                error_message=str(exc),
            )
        except Exception:
            pass
        raise
    return VisualProcessingResult(
        "ready",
        asset_count=assets,
        extraction_count=extractions,
        mode=("visual_text+siglip2" if semantic.state == "ready" else "visual_text"),
        semantic_state=semantic.state,
        semantic_image_count=semantic.image_count,
        semantic_page_count=semantic.page_count,
        semantic_error_code=semantic.error_code,
    )


__all__ = ["VISUAL_MANIFEST_VERSION", "VISUAL_STAGE_NAME", "VisualProcessingResult", "process_version_visuals"]
