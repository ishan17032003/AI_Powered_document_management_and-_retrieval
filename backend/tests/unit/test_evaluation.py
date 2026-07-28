from app.evaluation import assert_ci_gates, evaluate, load_jsonl
from pathlib import Path


def test_labeled_evaluation_metrics_and_gates(tmp_path):
    dataset = load_jsonl(Path(__file__).parents[2] / "evaluation/labeled_retrieval_rag_v1.jsonl")
    results = []
    for case in dataset:
        results.append({
            "case_id": case["case_id"],
            "retrieved_document_ids": case.get("relevant_document_ids", []),
            "citations": case.get("expected_citations", []),
            "claims": [{"supported": True} for _ in case.get("material_claims", [])],
        })
    metrics = evaluate(dataset, results)
    assert metrics["cases"] == 5
    assert metrics["unauthorized_evidence"] == 0
    assert_ci_gates(metrics)
