"""Pure lightweight auto-classifier — FR-OCR-02 (v0).

A transparent keyword/heuristic classifier that returns (class_name, confidence).
It is deliberately simple and pluggable: the production design replaces this
with Qwen3-VL on SageMaker while keeping the same call signature.
"""

from __future__ import annotations

import re

# class -> signal keywords (lowercased)
_RULES: dict[str, list[str]] = {
    "Invoice": [
        "invoice",
        "amount due",
        "gst",
        "tax invoice",
        "bill to",
        "invoice no",
        "subtotal",
    ],
    "Contract": [
        "agreement",
        "hereby",
        "party of the first",
        "terms and conditions",
        "witnesseth",
        "shall",
    ],
    "ID": [
        "aadhaar",
        "passport",
        "date of birth",
        "identity",
        "pan card",
        "driving licence",
    ],
    "Report": ["report", "summary", "findings", "conclusion", "analysis", "quarterly"],
    "Purchase Order": [
        "purchase order",
        "po number",
        "vendor",
        "ship to",
        "delivery date",
    ],
    "Correspondence": ["dear sir", "regards", "sincerely", "re:", "subject:"],
}

DEFAULT_CLASS = "Uncategorized"
_TOKEN = re.compile(r"[a-z0-9]+")


def classify(text: str) -> tuple[str, float]:
    if not text or not text.strip():
        return DEFAULT_CLASS, 0.0

    low = text.lower()
    scores: dict[str, int] = {}
    for cls, kws in _RULES.items():
        hits = sum(1 for kw in kws if kw in low)
        if hits:
            scores[cls] = hits

    if not scores:
        return DEFAULT_CLASS, 0.2

    best = max(scores, key=scores.__getitem__)
    # Confidence: hits for best relative to that class's keyword count, capped.
    denom = max(len(_RULES[best]), 1)
    confidence = round(min(0.5 + scores[best] / denom, 0.99), 3)
    return best, confidence
