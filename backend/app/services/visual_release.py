"""Offline evaluation and release-safety contracts for visual retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class VisualCitation:
    document_id: int
    version_id: int | str
    page_number: int
    asset_id: int
    lineage: str


def validate_visual_citation(value: Mapping[str, object], allowed_document_ids: frozenset[int]) -> VisualCitation | None:
    """Accept only complete document/version/page/asset lineage citations."""
    document_id = value.get("document_id")
    version_id = value.get("version_id")
    page_number = value.get("page_number")
    asset_id = value.get("asset_id")
    lineage = value.get("lineage")
    if type(document_id) is not int or document_id not in allowed_document_ids:
        return None
    if type(version_id) not in {int, str} or isinstance(version_id, bool) or not str(version_id):
        return None
    if type(page_number) is not int or page_number < 1 or type(asset_id) is not int or asset_id < 1:
        return None
    if not isinstance(lineage, str) or not lineage or len(lineage) > 300:
        return None
    return VisualCitation(document_id, version_id, page_number, asset_id, lineage)


@dataclass(frozen=True, slots=True)
class VisualEvaluationCase:
    case_id: str
    mode: str
    expected_asset_ids: frozenset[int]
    allowed_document_ids: frozenset[int]
    requires_abstention: bool = False
    adversarial: bool = False


def validate_evaluation_corpus(cases: Iterable[VisualEvaluationCase], *, expected_size: int = 600) -> tuple[VisualEvaluationCase, ...]:
    values = tuple(cases)
    if len(values) != expected_size or len({case.case_id for case in values}) != expected_size:
        raise ValueError(f"visual evaluation corpus must contain {expected_size} unique cases")
    if any(case.mode not in {"text_to_page", "text_to_image", "image_to_image", "hybrid"} for case in values):
        raise ValueError("visual evaluation corpus contains an unsupported mode")
    return values


@dataclass(frozen=True, slots=True)
class VisualEvaluationReport:
    recall: float
    groundedness: float
    abstention_accuracy: float
    security_zero_leakage: bool
    p95_latency_ms: float


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]


def evaluate_visual_cases(
    cases: Iterable[VisualEvaluationCase],
    results: Mapping[str, Iterable[Mapping[str, object]]],
    latencies_ms: Mapping[str, float],
) -> VisualEvaluationReport:
    values = tuple(cases)
    if not values:
        raise ValueError("visual evaluation cases are required")
    recall_values: list[float] = []
    grounded_values: list[float] = []
    abstention_values: list[float] = []
    leaked = False
    for case in values:
        rows = tuple(results.get(case.case_id, ()))
        returned_assets = {row.get("asset_id") for row in rows if type(row.get("asset_id")) is int}
        allowed = case.allowed_document_ids
        leaked |= any(row.get("document_id") not in allowed for row in rows)
        recall_values.append(len(returned_assets & case.expected_asset_ids) / len(case.expected_asset_ids) if case.expected_asset_ids else 1.0)
        grounded_values.append(sum(1 for row in rows if row.get("asset_id") in case.expected_asset_ids) / len(rows) if rows else 1.0)
        abstention_values.append(float(bool(not rows) == case.requires_abstention))
    return VisualEvaluationReport(round(mean(recall_values), 4), round(mean(grounded_values), 4), round(mean(abstention_values), 4), not leaked, round(_p95([float(value) for value in latencies_ms.values()]), 3))


@dataclass(slots=True)
class BackfillCursor:
    completed_keys: set[str] = field(default_factory=set)
    last_key: str | None = None


def run_resumable_backfill(
    items: Iterable[tuple[str, object]],
    cursor: BackfillCursor,
    process: Callable[[str, object], None],
    *,
    batch_size: int = 100,
) -> BackfillCursor:
    """Process authoritative items in stable order; completed keys are replay-safe."""
    if not 1 <= batch_size <= 10_000:
        raise ValueError("invalid backfill batch size")
    processed = 0
    for key, item in sorted(items, key=lambda value: value[0]):
        if key in cursor.completed_keys:
            continue
        process(key, item)
        cursor.completed_keys.add(key)
        cursor.last_key = key
        processed += 1
        if processed >= batch_size:
            break
    return cursor


@dataclass(frozen=True, slots=True)
class ShadowReport:
    queries: int
    disagreements: int
    overlap_ratio: float
    average_latency_ms: float


def compare_shadow_queries(pairs: Iterable[tuple[set[int], set[int], float]]) -> ShadowReport:
    values = list(pairs)
    if not values:
        return ShadowReport(0, 0, 0.0, 0.0)
    overlap = sum(len(primary & shadow) / max(1, len(primary)) for primary, shadow, _ in values)
    return ShadowReport(len(values), sum(primary != shadow for primary, shadow, _ in values), round(overlap / len(values), 4), round(mean(max(0.0, latency) for _, _, latency in values), 3))


@dataclass(frozen=True, slots=True)
class CanaryDecision:
    enabled: bool
    rollback_required: bool
    reason: str


def evaluate_canary(*, error_rate: float, quality: float, max_error_rate: float = 0.02, min_quality: float = 0.7) -> CanaryDecision:
    rollback = error_rate > max_error_rate or quality < min_quality
    return CanaryDecision(not rollback, rollback, "threshold_breach" if rollback else "within_budget")


__all__ = ["BackfillCursor", "CanaryDecision", "ShadowReport", "VisualCitation", "VisualEvaluationCase", "VisualEvaluationReport", "compare_shadow_queries", "evaluate_canary", "evaluate_visual_cases", "run_resumable_backfill", "validate_evaluation_corpus", "validate_visual_citation"]
