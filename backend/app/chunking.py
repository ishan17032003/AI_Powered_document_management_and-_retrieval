"""Deterministic, bounded text chunking with citation lineage.

The chunker deliberately operates on plain text and source spans.  Extractors
can provide page/section/table metadata for each span; that metadata is copied
to every emitted chunk so a retriever can cite the original source safely.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 3_500
DEFAULT_OVERLAP_CHARS = 350
CHUNKER_VERSION = "docling-deterministic-v1"



@dataclass(frozen=True, slots=True)
class SourceSegment:
    """An extracted source range and its citation lineage."""

    text: str
    start: int
    end: int
    page_start: int | None = None
    page_end: int | None = None
    section_path: tuple[str, ...] = ()
    chunk_type: str = "prose"


@dataclass(frozen=True, slots=True)
class TextChunk:
    chunk_id: str
    text: str
    chunk_no: int
    source_start: int
    source_end: int
    page_start: int | None
    page_end: int | None
    section_path: tuple[str, ...]
    chunk_type: str


def _stable_id(document_id: int | str, version_id: int | str, no: int, text: str) -> str:
    digest = hashlib.sha256(
        f"{document_id}\x00{version_id}\x00{no}\x00{text}".encode("utf-8")
    ).hexdigest()[:32]
    return f"chunk_{digest}"


def _split_bounded(text: str, max_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Split on whitespace where possible while guaranteeing the bound."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        end = min(cursor + max_chars, length)
        if end < length:
            boundary = max(
                text.rfind(" ", cursor + max_chars // 2, end),
                text.rfind("\n", cursor + max_chars // 2, end),
            )
            if boundary > cursor:
                end = boundary
        end = max(end, cursor + 1)
        spans.append((cursor, end))
        if end >= length:
            break
        cursor = max(0, end - overlap_chars)
    return spans


def chunk_text(
    document_id: int | str,
    version_id: int | str,
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    source_segments: Sequence[SourceSegment] | None = None,
) -> list[TextChunk]:
    """Return deterministic chunks with bounded text and source lineage.

    ``source_segments`` may describe pages, tables, or headings.  When absent,
    one prose segment covering the input is used.  Segments are never merged
    across a source boundary, preventing a citation from pointing at unrelated
    pages or table cells.
    """
    if type(max_chars) is not int or max_chars < 128:
        raise ValueError("max_chars must be an integer >= 128")
    if type(overlap_chars) is not int or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be in [0, max_chars)")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    segments = list(source_segments or [SourceSegment(text, 0, len(text))])
    output: list[TextChunk] = []
    for segment in segments:
        if not segment.text.strip():
            continue
        for local_start, local_end in _split_bounded(segment.text, max_chars, overlap_chars):
            value = segment.text[local_start:local_end].strip()
            if not value:
                continue
            # Keep offsets aligned to the original extracted source after trim.
            leading = len(segment.text[local_start:local_end]) - len(
                segment.text[local_start:local_end].lstrip()
            )
            trailing = len(segment.text[local_start:local_end].rstrip())
            start = segment.start + local_start + leading
            end = segment.start + local_start + trailing
            no = len(output)
            output.append(
                TextChunk(
                    chunk_id=_stable_id(document_id, version_id, no, value),
                    text=value,
                    chunk_no=no,
                    source_start=start,
                    source_end=end,
                    page_start=segment.page_start,
                    page_end=segment.page_end,
                    section_path=segment.section_path,
                    chunk_type=segment.chunk_type,
                )
            )
    return output


def chunk_docling(
    document_id: int | str,
    version_id: int | str,
    pages: object,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[TextChunk]:
    """Adapt Docling-exported pages/elements to :func:`chunk_text`.

    The adapter is dependency-free and accepts the common ``pages``/``elements``
    mapping shape emitted by Docling exporters.  Tables and headings remain
    separate source segments, preserving citation lineage and section paths.
    """
    if isinstance(pages, dict):
        pages = pages.get("pages") or pages.get("elements") or []
    segments: list[SourceSegment] = []
    offset = 0
    section: list[str] = []
    for page_no, page in enumerate(pages if isinstance(pages, Sequence) and not isinstance(pages, (str, bytes)) else [], 1):
        if not isinstance(page, dict):
            continue
        number = int(page.get("page") or page.get("page_no") or page_no)
        blocks = page.get("elements") or page.get("blocks") or [page]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            value = block.get("text") or block.get("content") or ""
            if not value and isinstance(block.get("rows"), Sequence):
                rows = block["rows"]
                rendered: list[str] = []
                for row in rows:
                    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
                        rendered.append("| " + " | ".join(str(cell) for cell in row) + " |")
                    elif isinstance(row, Mapping):
                        rendered.append("| " + " | ".join(str(cell) for cell in row.values()) + " |")
                value = "\n".join(rendered)
            if not isinstance(value, str) or not value.strip():
                continue
            kind = str(block.get("type") or block.get("kind") or "paragraph").lower()
            if "heading" in kind or kind in {"title", "section_header"}:
                level = max(1, int(block.get("level") or block.get("heading_level") or 1))
                section[:] = section[: level - 1]
                section.append(value.strip())
                continue
            chunk_type = "table" if "table" in kind else ("list" if "list" in kind else "prose")
            clean = value.strip()
            segments.append(SourceSegment(clean, offset, offset + len(clean), number, number, tuple(section), chunk_type))
            offset += len(clean) + 1
    bounded_overlap = min(overlap_chars, max_chars - 1)
    return chunk_text(document_id, version_id, "\n".join(segment.text for segment in segments), max_chars=max_chars, overlap_chars=bounded_overlap, source_segments=segments)


__all__ = ["CHUNKER_VERSION", "SourceSegment", "TextChunk", "chunk_text", "chunk_docling"]
