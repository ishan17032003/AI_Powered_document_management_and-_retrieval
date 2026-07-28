"""Validate the non-secret maturity evidence scaffolding."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERY_SAMPLE = ROOT / "maturity/evaluation/production-like-query-sample.jsonl"
LEDGER = ROOT / "maturity/evidence-ledger-template.json"
MATURITY_ARTIFACTS = {
    "MAT-015": ROOT / "maturity/exercises/provider-unavailability-and-no-egress.md",
    "MAT-016": ROOT / "maturity/reviews/independent-assurance-review.md",
    "MAT-017": ROOT / "maturity/reviews/quarterly-score-review-template.json",
    "MAT-018": ROOT / "maturity/reviews/annual-capacity-risk-architecture-review.md",
    "MAT-019": ROOT / "maturity/reviews/independent-10-of-10-panel.md",
}
REQUIRED_CASE_FIELDS = {"case_id", "slice", "query", "answerable", "review_status", "protected_content"}


def validate() -> list[str]:
    failures: list[str] = []
    cases = [json.loads(line) for line in QUERY_SAMPLE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(cases) < 8 or len({case.get("case_id") for case in cases}) != len(cases):
        failures.append("query sample must contain at least 8 unique cases")
    for case in cases:
        missing = REQUIRED_CASE_FIELDS - set(case)
        if missing:
            failures.append(f"{case.get('case_id', '<unknown>')} missing {sorted(missing)}")
        if case.get("protected_content") != "synthetic_only":
            failures.append(f"{case.get('case_id', '<unknown>')} is not synthetic-only")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    required = {"task_id", "release_id", "corpus_revision", "model_revision", "index_revision", "owner", "evidence_uri", "decision"}
    if set(ledger.get("record_fields", [])) < required:
        failures.append("evidence ledger omits required release/model/index ownership fields")
    for task_id, path in MATURITY_ARTIFACTS.items():
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            failures.append(f"{task_id} artifact is missing or empty: {path.relative_to(ROOT)}")
    quarterly = MATURITY_ARTIFACTS["MAT-017"]
    if quarterly.is_file():
        payload = json.loads(quarterly.read_text(encoding="utf-8"))
        required_quarterly = {"quarter", "evidence_window", "domains", "blockers", "expired_evidence", "decision", "next_eligibility_date"}
        if not required_quarterly.issubset(payload):
            failures.append("MAT-017 quarterly score template omits blocker/expiry/eligibility fields")
    return failures


if __name__ == "__main__":
    failures = validate()
    if failures:
        for failure in failures:
            print(f"MATURITY_INVALID: {failure}")
        raise SystemExit(78)
    print("MAT-006–019 artifact checks passed")
