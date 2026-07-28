"""Privacy policy hooks for ephemeral query images (MM-003).

Query images are untrusted, short-lived evidence.  This module deliberately
does not persist bytes or permit provider egress by default; deployment policy
must explicitly approve both retention and destination.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping


class QueryImagePolicyError(ValueError):
    """Raised when a query-image operation violates the privacy policy."""


@dataclass(frozen=True, slots=True)
class QueryImagePolicy:
    retention_seconds: int = 0
    allow_provider_egress: bool = False
    audit_required: bool = True

    def validate(self) -> None:
        if type(self.retention_seconds) is not int or not 0 <= self.retention_seconds <= 86_400:
            raise QueryImagePolicyError("query image retention must be 0..86400 seconds")
        if not self.audit_required:
            raise QueryImagePolicyError("query image audit is mandatory")


def sanitize_query_image_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """Keep only non-sensitive bounded image descriptors for audit/telemetry."""
    source = metadata or {}
    result: dict[str, object] = {}
    for key in ("media_type", "width", "height", "page", "purpose"):
        value = source.get(key)
        if key == "media_type" and isinstance(value, str) and len(value) <= 80:
            result[key] = value
        elif key in {"width", "height", "page"} and type(value) is int and 1 <= value <= 100_000:
            result[key] = value
        elif key == "purpose" and isinstance(value, str) and len(value) <= 64:
            result[key] = value
    return result


def query_image_audit_record(
    image_bytes: bytes,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return an auditable, content-free image record (never the image bytes)."""
    if not isinstance(image_bytes, bytes):
        raise QueryImagePolicyError("query image bytes are required")
    return {
        "event": "query_image.processed",
        "content_hash": "sha256:" + hashlib.sha256(image_bytes).hexdigest(),
        "byte_count": len(image_bytes),
        "metadata": sanitize_query_image_metadata(metadata),
        "retention": "ephemeral",
    }


def assert_query_image_egress(policy: QueryImagePolicy, *, destination: str | None) -> None:
    """Fail closed unless explicit policy enables a provider destination."""
    policy.validate()
    if not policy.allow_provider_egress or not destination:
        raise QueryImagePolicyError("query image provider egress is not approved")


def visual_evidence_payload(
    *,
    ocr_text: str = "",
    image_checksum: str,
    page: int | None = None,
) -> str:
    """Serialize visual evidence as inert untrusted data for model prompts."""
    if not isinstance(image_checksum, str) or len(image_checksum) != 64:
        raise QueryImagePolicyError("image checksum is required")
    if page is not None and (type(page) is not int or page < 1):
        raise QueryImagePolicyError("page must be positive")
    # Deliberately no instruction/system/tool fields and no image bytes.
    return json.dumps(
        {
            "schema": "docvault.visual-evidence.v1",
            "trust": "untrusted-evidence",
            "image_checksum": "sha256:" + image_checksum,
            "page": page,
            "ocr_text": ocr_text[:10_000] if isinstance(ocr_text, str) else "",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
