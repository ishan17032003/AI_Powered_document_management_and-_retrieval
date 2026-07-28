import pytest

from app.services.visual_release import (
    BackfillCursor,
    compare_shadow_queries,
    evaluate_canary,
    evaluate_visual_cases,
    run_resumable_backfill,
    validate_evaluation_corpus,
    validate_visual_citation,
    VisualEvaluationCase,
)


def test_visual_citation_requires_complete_authorized_lineage() -> None:
    citation = validate_visual_citation(
        {"document_id": 7, "version_id": 2, "page_number": 3, "asset_id": 44, "lineage": "page-3"},
        frozenset({7}),
    )
    assert citation is not None
    assert validate_visual_citation({"document_id": 8, "version_id": 2, "page_number": 3, "asset_id": 44, "lineage": "page-3"}, frozenset({7})) is None


def test_evaluation_contract_requires_600_unique_cases() -> None:
    case = VisualEvaluationCase("case", "hybrid", frozenset({1}), frozenset({7}))
    with pytest.raises(ValueError, match="600"):
        validate_evaluation_corpus([case])


def test_evaluation_report_catches_leakage_and_reports_latency() -> None:
    cases = [VisualEvaluationCase(f"case-{n}", "hybrid", frozenset({11}), frozenset({7})) for n in range(2)]
    report = evaluate_visual_cases(
        cases,
        {"case-0": [{"asset_id": 1, "document_id": 7}], "case-1": [{"asset_id": 1, "document_id": 99}]},
        {"case-0": 10.0, "case-1": 20.0},
    )
    assert report.security_zero_leakage is False
    assert report.p95_latency_ms == 10.0


def test_backfill_is_resumable_and_idempotent() -> None:
    cursor = BackfillCursor()
    calls: list[str] = []
    items = [("b", 2), ("a", 1), ("c", 3)]
    run_resumable_backfill(items, cursor, lambda key, _value: calls.append(key), batch_size=2)
    run_resumable_backfill(items, cursor, lambda key, _value: calls.append(key), batch_size=2)
    assert calls == ["a", "b", "c"]
    assert cursor.last_key == "c"


def test_shadow_and_canary_contracts_are_fail_closed() -> None:
    shadow = compare_shadow_queries([({1, 2}, {1}, 4.0), ({1}, {1}, 6.0)])
    assert shadow.disagreements == 1
    assert evaluate_canary(error_rate=0.03, quality=0.9).rollback_required is True
    assert evaluate_canary(error_rate=0.01, quality=0.9).enabled is True
