"""Redacted visual telemetry and bounded feedback intake (MM-040)."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from ..config import settings

_LANES = {"text", "page", "image", "hybrid"}
_OUTCOMES = {"success", "empty", "error", "rejected", "saturated"}
_FEEDBACK_CATEGORIES = {"relevant", "irrelevant", "unauthorized", "unsafe", "wrong_type"}


def _digest(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    key = settings.secret_key.encode("utf-8")
    if not key:
        return None
    return "hmac-sha256:" + hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class VisualTelemetry:
    lane: str
    outcome: str
    model_revision: str | None
    index_revision: str | None
    policy_revision: int | None
    candidate_count: int
    authorized_count: int
    duration_ms: float
    query_digest: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "outcome": self.outcome,
            "model_revision": self.model_revision,
            "index_revision": self.index_revision,
            "policy_revision": self.policy_revision,
            "candidate_count": self.candidate_count,
            "authorized_count": self.authorized_count,
            "duration_ms": self.duration_ms,
            "query_digest": self.query_digest,
        }


def build_visual_telemetry(
    *,
    lane: str,
    outcome: str,
    query: str | None = None,
    model_revision: str | None = None,
    index_revision: str | None = None,
    policy_revision: int | None = None,
    candidate_count: int = 0,
    authorized_count: int = 0,
    duration_ms: float = 0.0,
) -> VisualTelemetry:
    if lane not in _LANES or outcome not in _OUTCOMES:
        raise ValueError("invalid visual telemetry category")
    if type(policy_revision) not in (int, type(None)) or (policy_revision is not None and policy_revision < 0):
        raise ValueError("invalid policy revision")
    return VisualTelemetry(
        lane=lane,
        outcome=outcome,
        model_revision=model_revision[:160] if isinstance(model_revision, str) else None,
        index_revision=index_revision[:160] if isinstance(index_revision, str) else None,
        policy_revision=policy_revision,
        candidate_count=max(0, min(int(candidate_count), 100_000)),
        authorized_count=max(0, min(int(authorized_count), 100_000)),
        duration_ms=round(max(0.0, min(float(duration_ms), 86_400_000.0)), 3),
        query_digest=_digest(query),
    )


@dataclass(frozen=True, slots=True)
class VisualFeedback:
    result_id: str
    category: str
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.result_id or len(self.result_id) > 128:
            raise ValueError("result_id is required and bounded")
        if self.category not in _FEEDBACK_CATEGORIES:
            raise ValueError("invalid feedback category")
        if self.note is not None and len(self.note) > 240:
            raise ValueError("feedback note is too long")

    def audit_fields(self) -> dict[str, str]:
        return {
            "result_digest": _digest(self.result_id) or "redacted",
            "category": self.category,
        }


__all__ = ["VisualFeedback", "VisualTelemetry", "build_visual_telemetry"]
