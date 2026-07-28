"""Read-only visual retrieval projections.

The repository returns only derived visual metadata and untrusted extraction
text.  It never decides whether a caller may see a document; callers must
provide the exact SQL-authorized document-ID set and reauthorize assets before
hydration.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


@dataclass(frozen=True, slots=True)
class VisualSearchCandidate:
    asset_id: int
    document_id: int
    version_id: int
    title: str
    asset_type: str
    page_number: int | None
    content_type: str
    checksum: str
    extraction_text: str
    extraction_types: tuple[str, ...]


def list_candidates(
    db: Session,
    *,
    document_ids: frozenset[int],
    asset_types: frozenset[str],
    limit: int,
) -> list[VisualSearchCandidate]:
    """Return bounded active visual candidates for one authorized ID set."""

    if not document_ids or not asset_types or not 1 <= limit <= 10_000:
        return []

    rows = db.execute(
        select(models.VisualAsset, models.Document)
        .join(models.Document, models.Document.id == models.VisualAsset.document_id)
        .where(
            models.VisualAsset.document_id.in_(document_ids),
            models.VisualAsset.asset_type.in_(asset_types),
            models.VisualAsset.lifecycle_state == "ACTIVE",
            models.Document.lifecycle_state == "ACTIVE",
        )
        .order_by(models.VisualAsset.id)
        .limit(limit)
    ).all()
    if not rows:
        return []

    asset_ids = {asset.id for asset, _document in rows}
    extractions = db.scalars(
        select(models.VisualExtraction)
        .where(models.VisualExtraction.asset_id.in_(asset_ids))
        .order_by(models.VisualExtraction.id)
    ).all()
    grouped: defaultdict[int, list[models.VisualExtraction]] = defaultdict(list)
    for extraction in extractions:
        grouped[extraction.asset_id].append(extraction)

    return _build_candidates(rows, grouped)


def _build_candidates(
    rows: list[tuple[models.VisualAsset, models.Document]],
    grouped: defaultdict[int, list[models.VisualExtraction]],
) -> list[VisualSearchCandidate]:
    candidates: list[VisualSearchCandidate] = []
    for asset, document in rows:
        values = grouped.get(asset.id, [])
        text_parts = [value.text.strip()[:500_000] for value in values if value.text]
        extraction_types = tuple(dict.fromkeys(value.output_type for value in values))
        candidates.append(
            VisualSearchCandidate(
                asset_id=asset.id,
                document_id=asset.document_id,
                version_id=asset.version_id,
                title=document.title,
                asset_type=asset.asset_type,
                page_number=asset.page_number,
                content_type=asset.content_type,
                checksum=asset.checksum,
                extraction_text="\n".join(text_parts),
                extraction_types=extraction_types,
            )
        )
    return candidates


def candidates_by_asset_ids(
    db: Session,
    *,
    asset_ids: set[int],
    document_ids: frozenset[int],
    asset_types: frozenset[str],
) -> list[VisualSearchCandidate]:
    """Hydrate only a bounded semantic hit set after the vector search."""

    if not asset_ids or not document_ids or not asset_types:
        return []
    valid_ids = {value for value in asset_ids if type(value) is int and value > 0}
    if not valid_ids:
        return []
    rows = db.execute(
        select(models.VisualAsset, models.Document)
        .join(models.Document, models.Document.id == models.VisualAsset.document_id)
        .where(
            models.VisualAsset.id.in_(valid_ids),
            models.VisualAsset.document_id.in_(document_ids),
            models.VisualAsset.asset_type.in_(asset_types),
            models.VisualAsset.lifecycle_state == "ACTIVE",
            models.Document.lifecycle_state == "ACTIVE",
        )
        .order_by(models.VisualAsset.id)
        .limit(min(len(valid_ids), 10_000))
    ).all()
    if not rows:
        return []
    extractions = db.scalars(
        select(models.VisualExtraction)
        .where(models.VisualExtraction.asset_id.in_({asset.id for asset, _ in rows}))
        .order_by(models.VisualExtraction.id)
    ).all()
    grouped: defaultdict[int, list[models.VisualExtraction]] = defaultdict(list)
    for extraction in extractions:
        grouped[extraction.asset_id].append(extraction)
    return _build_candidates(rows, grouped)


def candidates_by_checksum(
    db: Session,
    *,
    payload: bytes,
    document_ids: frozenset[int],
    asset_types: frozenset[str],
    limit: int,
) -> list[VisualSearchCandidate]:
    """Return exact active image candidates for an ephemeral normalized payload."""

    if not payload or not document_ids or not asset_types or not 1 <= limit <= 100:
        return []
    checksum = hashlib.sha256(payload).hexdigest()
    rows = db.execute(
        select(models.VisualAsset, models.Document)
        .join(models.Document, models.Document.id == models.VisualAsset.document_id)
        .where(
            models.VisualAsset.checksum == checksum,
            models.VisualAsset.document_id.in_(document_ids),
            models.VisualAsset.asset_type.in_(asset_types),
            models.VisualAsset.lifecycle_state == "ACTIVE",
            models.Document.lifecycle_state == "ACTIVE",
        )
        .order_by(models.VisualAsset.id)
        .limit(limit)
    ).all()
    if not rows:
        return []
    asset_ids = {asset.id for asset, _document in rows}
    extractions = db.scalars(
        select(models.VisualExtraction)
        .where(models.VisualExtraction.asset_id.in_(asset_ids))
        .order_by(models.VisualExtraction.id)
    ).all()
    grouped: defaultdict[int, list[models.VisualExtraction]] = defaultdict(list)
    for extraction in extractions:
        grouped[extraction.asset_id].append(extraction)
    return _build_candidates(rows, grouped)


__all__ = [
    "VisualSearchCandidate",
    "candidates_by_asset_ids",
    "candidates_by_checksum",
    "list_candidates",
]
