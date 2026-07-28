"""Final SQL reauthorization and protected visual derivative access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..repositories.visible_document_repository import resolve_visible_document_ids
from ..storage import object_store


class VisualAccessDenied(LookupError):
    """Safe not-found equivalent for unauthorized or stale visual assets."""


@dataclass(frozen=True, slots=True)
class VisualPreview:
    asset_id: int
    content_type: str
    filename: str
    body: bytes


def reauthorize_visual_assets(
    db: Session,
    *,
    user_id: int,
    asset_ids: set[int] | frozenset[int],
) -> list[models.VisualAsset]:
    """Hydrate only active assets whose parent document is currently VIEWable."""
    if not asset_ids:
        return []
    allowed_documents = resolve_visible_document_ids(
        db,
        user_id=user_id,
        permission="VIEW",
        now=datetime.now(UTC),
    )
    if not allowed_documents:
        return []
    assets = db.scalars(
        select(models.VisualAsset).where(
            models.VisualAsset.id.in_(asset_ids),
            models.VisualAsset.document_id.in_(allowed_documents),
            models.VisualAsset.lifecycle_state == "ACTIVE",
        )
    ).all()
    return [asset for asset in assets if asset.document_id in allowed_documents]


def open_authorized_preview(db: Session, *, user_id: int, asset_id: int) -> VisualPreview:
    """Read a derivative only after a fresh SQL authorization and lifecycle check."""
    assets = reauthorize_visual_assets(db, user_id=user_id, asset_ids={asset_id})
    if len(assets) != 1:
        raise VisualAccessDenied("visual preview not found")
    asset = assets[0]
    try:
        with object_store.open(asset.file_key) as handle:
            body = handle.read()
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise VisualAccessDenied("visual preview not found") from exc
    if len(body) != asset.size:
        raise VisualAccessDenied("visual preview unavailable")
    return VisualPreview(
        asset_id=asset.id,
        content_type=asset.content_type,
        filename=f"visual-{asset.id}",
        body=body,
    )


__all__ = ["VisualAccessDenied", "VisualPreview", "open_authorized_preview", "reauthorize_visual_assets"]
