from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path


PLAN_DIR = Path(__file__).resolve().parent
PARTS = sorted(PLAN_DIR.glob("[0-9][0-9]-*.md"))
EXPECTED_PARTS = {
    "00-program-charter-decisions.md": (
        "## Executive answer",
        "## 1. Goal and completion definition",
        "## 2. Default decisions and decision locks",
        "## 3. Target architecture",
        "## 4. Engineering rules for every work item",
        "## 5. Delivery sequence and critical path",
    ),
    "01-foundation-build-containment.md": ("## WP-00", "## WP-01", "## WP-02"),
    "02-import-schema-integrity.md": ("## WP-04", "## WP-05"),
    "03-authorization-identity.md": ("## WP-03",),
    "04-ingestion-storage-deletion.md": ("## WP-06",),
    "05-audit-recovery-dr.md": ("## WP-07",),
    "06-text-retrieval-lancedb.md": ("## WP-08",),
    "07-rag-safety-quality.md": ("## WP-09",),
    "08-multimodal-scope-architecture.md": (
        "## Executive conclusion",
        "## 1. Research basis and limits",
        "## 2. Product behavior in scope",
        "## 3. Target retrieval architecture",
    ),
    "09-visual-data-ingestion.md": (
        "## 4. Durable data and index model",
        "## 5. Secure visual ingestion",
    ),
    "10-visual-models-capacity.md": ("## 6. Model-selection and retrieval decisions",),
    "11-multimodal-authorization-security.md": (
        "## 7. Flexible per-user file authorization",
        "## 8. RAG and multimodal security",
    ),
    "12-evaluation-observability-rollout.md": (
        "## 9. Evaluation program",
        "## 10. Observability and service objectives",
        "## 11. Rollout, rollback, and rebuild",
    ),
    "13-multimodal-work-package.md": ("## 12. WP-13", "### WP-13 exit"),
    "14-api-performance-capacity.md": ("## WP-10",),
    "15-operations-release.md": ("## WP-11", "## WP-12"),
    "16-operational-maturity-rating.md": (
        "## 13. WP-14",
        "## 14. Rating and evidence ladder",
        "## 15. Dependency and delivery sequence",
    ),
    "17-code-map-iterations-ownership.md": (
        "## 7. Planned code and repository change map",
        "## 8. First two implementation iterations",
        "## 9. Ownership and review model",
    ),
    "18-traceability-checklists.md": (
        "## 10. Traceability to production blockers",
        "## 11. Traceability to backend audit areas",
        "## 12. Projected rating after complete implementation",
        "## 13. Final program checklist",
        "## 14. Plan maintenance",
    ),
    "19-research-sources.md": (
        "## 16. Research source register",
        "## 17. Final answer on plan completeness",
    ),
}
REFERENCES = [
    PLAN_DIR / "reference" / "BACKEND_IMPROVEMENT_IMPLEMENTATION_PLAN_v1.1.md",
    PLAN_DIR / "reference" / "BACKEND_RAG_IMAGE_SEARCH_MATURITY_PLAN_v1.0.md",
]
TASK_PATTERN = re.compile(r"^\| ([A-Z][A-Z0-9]*-\d{3}) \|", re.MULTILINE)
LINK_PATTERN = re.compile(r"\]\((?:<)?([^)>]+)(?:>)?\)")
VALID_STATUSES = {"not_started", "in_progress", "blocked", "done", "not_applicable"}
MAX_PART_LINES = 320


errors: list[str] = []

actual_part_names = {part.name for part in PARTS}
for part_name in sorted(set(EXPECTED_PARTS) - actual_part_names):
    errors.append(f"missing numbered part: {part_name}")
for part_name in sorted(actual_part_names - set(EXPECTED_PARTS)):
    errors.append(f"unexpected numbered part: {part_name}")

for reference in REFERENCES:
    if not reference.exists():
        errors.append(f"missing frozen reference: {reference.relative_to(PLAN_DIR)}")

expected: list[str] = []
for reference in REFERENCES:
    if reference.exists():
        expected.extend(TASK_PATTERN.findall(reference.read_text(encoding="utf-8")))

actual_locations: dict[str, list[str]] = {}
for part in PARTS:
    text = part.read_text(encoding="utf-8")
    line_count = len(text.splitlines())
    if line_count > MAX_PART_LINES:
        errors.append(f"{part.name} has {line_count} lines; limit is {MAX_PART_LINES}")
    if text.count(chr(96) * 3) % 2:
        errors.append(f"unbalanced code fence: {part.name}")
    for required_anchor in EXPECTED_PARTS.get(part.name, ()):
        if required_anchor not in text:
            errors.append(f"required section missing from {part.name}: {required_anchor}")
    for task_id in TASK_PATTERN.findall(text):
        actual_locations.setdefault(task_id, []).append(part.name)

expected_counts = Counter(expected)
if len(expected_counts) != 215:
    errors.append(f"frozen references define {len(expected_counts)} unique tasks, expected 215")
for task_id, count in expected_counts.items():
    if count != 1:
        errors.append(f"frozen task appears {count} times: {task_id}")

actual_ids = set(actual_locations)
expected_ids = set(expected_counts)
for task_id in sorted(expected_ids - actual_ids):
    errors.append(f"task missing from modular parts: {task_id}")
for task_id in sorted(actual_ids - expected_ids):
    errors.append(f"unexpected modular task: {task_id}")
for task_id, locations in sorted(actual_locations.items()):
    if len(locations) != 1:
        errors.append(f"task owned by multiple parts: {task_id} -> {locations}")

ledger_path = PLAN_DIR / "TASK_STATUS.csv"
ledger_ids: list[str] = []
if not ledger_path.exists():
    errors.append("missing TASK_STATUS.csv")
else:
    with ledger_path.open(newline="", encoding="utf-8") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            task_id = row.get("task_id", "")
            ledger_ids.append(task_id)
            if row.get("status") not in VALID_STATUSES:
                errors.append(f"invalid ledger status at row {row_number}: {row.get('status')}")
            locations = actual_locations.get(task_id, [])
            if locations and row.get("part") != locations[0]:
                errors.append(
                    f"ledger part mismatch for {task_id}: {row.get('part')} != {locations[0]}"
                )

ledger_counts = Counter(ledger_ids)
for task_id in sorted(expected_ids - set(ledger_counts)):
    errors.append(f"task missing from ledger: {task_id}")
for task_id in sorted(set(ledger_counts) - expected_ids):
    errors.append(f"unexpected ledger task: {task_id}")
for task_id, count in ledger_counts.items():
    if count != 1:
        errors.append(f"ledger task appears {count} times: {task_id}")

docs = [
    PLAN_DIR / "README.md",
    PLAN_DIR / "STATUS.md",
    PLAN_DIR.parent / "BACKEND_IMPROVEMENT_IMPLEMENTATION_PLAN.md",
    PLAN_DIR.parent / "BACKEND_RAG_IMAGE_SEARCH_MATURITY_PLAN.md",
    *PARTS,
]
for doc in docs:
    if not doc.exists():
        errors.append(f"missing plan document: {doc.name}")
        continue
    text = doc.read_text(encoding="utf-8")
    for target in LINK_PATTERN.findall(text):
        if (
            target.startswith(("http://", "https://", "mailto:", "#"))
            or "://" in target
        ):
            continue
        clean_target = target.split("#", 1)[0]
        if not clean_target:
            continue
        if not (doc.parent / clean_target).resolve().exists():
            errors.append(f"broken local link in {doc.name}: {target}")

rating_part = PLAN_DIR / "16-operational-maturity-rating.md"
if rating_part.exists() and "8.275 ≈ 8.3/10" not in rating_part.read_text(encoding="utf-8"):
    errors.append("canonical weighted rating calculation is missing or changed")

if errors:
    print("BACKEND PLAN VALIDATION: FAILED")
    for error in errors:
        print(f"- {error}")
    sys.exit(1)

print("BACKEND PLAN VALIDATION: PASSED")
print(f"- numbered parts: {len(PARTS)}")
print(f"- unique task IDs: {len(actual_ids)}")
print(f"- task-ledger rows: {len(ledger_ids)}")
print(f"- largest part: {max((len(p.read_text(encoding='utf-8').splitlines()), p.name) for p in PARTS)}")
