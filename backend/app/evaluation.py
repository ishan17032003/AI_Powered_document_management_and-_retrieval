"""Deterministic retrieval/RAG evaluation metrics over labeled cases."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("evaluation dataset is empty")
    return rows


def evaluate(dataset: list[dict[str, Any]], results: list[dict[str, Any]], *, k: int = 10) -> dict[str, Any]:
    by_case = {row["case_id"]: row for row in results}
    if len(by_case) != len(results):
        raise ValueError("evaluation results contain duplicate case IDs")
    recalls: list[float] = []
    ndcgs: list[float] = []
    citation_scores: list[float] = []
    grounded_scores: list[float] = []
    unauthorized = 0
    for case in dataset:
        result = by_case.get(case["case_id"], {})
        ranked = [int(value) for value in result.get("retrieved_document_ids", [])[:k]]
        expected = set(case.get("relevant_document_ids", []))
        recalls.append(1.0 if not expected and not ranked else len(expected.intersection(ranked)) / max(1, len(expected)))
        if expected:
            dcg = sum((1.0 if doc_id in expected else 0.0) / math.log2(index + 2) for index, doc_id in enumerate(ranked))
            ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(expected), k)))
            ndcgs.append(dcg / ideal if ideal else 0.0)
        else:
            ndcgs.append(1.0 if not ranked else 0.0)
        expected_citations = {tuple(item.items()) for item in case.get("expected_citations", [])}
        actual_citations = {tuple(item.items()) for item in result.get("citations", [])}
        citation_scores.append(len(expected_citations.intersection(actual_citations)) / max(1, len(expected_citations)) if expected_citations else (1.0 if not actual_citations else 0.0))
        claims = result.get("claims", [])
        grounded_scores.append(sum(1 for claim in claims if claim.get("supported") is True) / max(1, len(claims)) if claims else (1.0 if not case.get("material_claims") else 0.0))
        unauthorized += len(set(ranked).intersection(case.get("forbidden_document_ids", [])))
    return {
        "schema_version": "retrieval-rag-evaluation-v1",
        "cases": len(dataset),
        "recall_at_k": sum(recalls) / len(recalls),
        "ndcg_at_k": sum(ndcgs) / len(ndcgs),
        "citation_correctness": sum(citation_scores) / len(citation_scores),
        "groundedness": sum(grounded_scores) / len(grounded_scores),
        "unauthorized_evidence": unauthorized,
    }


def assert_ci_gates(metrics: dict[str, Any]) -> None:
    gates = {
        "recall_at_k": 0.90,
        "ndcg_at_k": 0.80,
        "citation_correctness": 0.95,
        "groundedness": 0.95,
    }
    failures = [f"{name}<{threshold}" for name, threshold in gates.items() if float(metrics.get(name, 0)) < threshold]
    if int(metrics.get("unauthorized_evidence", 0)) != 0:
        failures.append("unauthorized_evidence>0")
    if failures:
        raise AssertionError("evaluation gates failed: " + ", ".join(failures))
