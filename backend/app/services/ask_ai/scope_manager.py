"""Scope manager for Ask AI.

Translates mention-parser output into concrete document IDs (always
intersected with the caller's RBAC-authorized allowed_ids) and manages
scope transitions (new, replace, merge, clear).
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .mongo_models import ActiveScope, ScopedClass, ScopedDocument


# ── Scope removal detection ────────────────────────────────────────────────────

_REMOVAL_PATTERNS = [
    re.compile(r"\b(remove|clear|forget|stop|cancel|exit|leave|quit)\b.*\b(doc(?:ument)?|class|scope|context|file)\b", re.I),
    re.compile(r"\b(doc(?:ument)?|class|scope|context|file)\b.*\b(remove|clear|forget)\b", re.I),
    re.compile(r"\b(no\s+(?:more\s+)?(?:doc(?:ument)?|class|scope|context|file))\b", re.I),
    re.compile(r"\b(reset|deselect)\b", re.I),
]


def is_scope_removal_request(message: str) -> bool:
    """Return True if the message is asking to clear the active scope."""
    return any(p.search(message) for p in _REMOVAL_PATTERNS)


# ── Document ID resolution ────────────────────────────────────────────────────

def resolve_document_ids_by_title(
    db: Session,
    titles: list[str],
    allowed_ids: set[int] | None,
) -> list[ScopedDocument]:
    """Map document titles to ScopedDocument objects, RBAC-filtered.

    Uses case-insensitive partial matching against existing document titles.
    Only documents within ``allowed_ids`` are returned.
    """
    if not titles:
        return []

    from ...repositories import rag_repository

    all_docs = rag_repository.accessible_documents(db, allowed_ids)

    results: list[ScopedDocument] = []
    matched_ids: set[int] = set()
    for title in titles:
        title_lower = title.lower()
        best = None
        best_score = 0
        for doc in all_docs:
            if doc.id in matched_ids:
                continue
            doc_title_lower = doc.title.lower()
            # Exact match wins
            if doc_title_lower == title_lower:
                best = doc
                best_score = 2
                break
            # Substring match
            if title_lower in doc_title_lower or doc_title_lower in title_lower:
                score = len(title_lower) / max(len(doc_title_lower), 1)
                if score > best_score:
                    best = doc
                    best_score = score
        if best and best_score > 0:
            matched_ids.add(best.id)
            results.append(ScopedDocument(document_id=best.id, title=best.title))

    return results


def resolve_class_document_ids(
    db: Session,
    class_names: list[str],
    allowed_ids: set[int] | None,
) -> list[ScopedClass]:
    """Map class names to ScopedClass objects containing member document IDs.

    Only documents within ``allowed_ids`` are included.
    """
    if not class_names:
        return []

    from ...repositories import rag_repository

    all_docs = rag_repository.accessible_documents(db, allowed_ids)

    # Build a class_name → [doc] map from the accessible documents
    class_map: dict[str, list] = {}
    for doc in all_docs:
        if doc.doc_class:
            cn = doc.doc_class.name
            class_map.setdefault(cn, []).append(doc)

    results: list[ScopedClass] = []
    seen_class_ids: set[int] = set()
    for class_name in class_names:
        class_name_lower = class_name.lower()
        best_key = None
        best_docs: list = []
        for cn, docs in class_map.items():
            if cn.lower() == class_name_lower:
                best_key = cn
                best_docs = docs
                break
            if class_name_lower in cn.lower() and not best_key:
                best_key = cn
                best_docs = docs
        if best_key and best_docs:
            class_id = best_docs[0].doc_class.id if best_docs[0].doc_class else -1
            if class_id not in seen_class_ids:
                seen_class_ids.add(class_id)
                results.append(
                    ScopedClass(
                        class_id=class_id,
                        class_name=best_key,
                        document_ids=[d.id for d in best_docs],
                    )
                )

    return results


# ── Scope computation ─────────────────────────────────────────────────────────


def compute_next_scope(
    current_scope: ActiveScope,
    mentions: dict,
    db: Session,
    allowed_ids: set[int] | None,
) -> tuple[ActiveScope, bool]:
    """Compute the next active scope given the current scope and new mentions.

    Returns (new_scope, scope_changed: bool).

    Rules:
    - If mentions contain new documents/classes → replace the scope entirely
      (a new @doc:{X} is always a deliberate context switch).
    - If no mentions → carry the current scope forward unchanged.
    - The returned scope always intersects with the caller's allowed_ids.
    """
    doc_names: list[str] = mentions.get("documents", [])
    class_names: list[str] = mentions.get("classes", [])

    if not doc_names and not class_names:
        # No new scope signal — carry forward unchanged
        return current_scope, False

    new_docs = resolve_document_ids_by_title(db, doc_names, allowed_ids)
    new_classes = resolve_class_document_ids(db, class_names, allowed_ids)
    new_scope = ActiveScope(documents=new_docs, classes=new_classes)
    scope_changed = (new_scope.model_dump() != current_scope.model_dump())
    return new_scope, scope_changed
