"""Company KB Agent — wraps the existing rag_service.ask().

Applies the active conversation scope to restrict retrieval without modifying
rag_service.py itself.  RBAC is always enforced: the agent never passes an
allowed_ids set that is larger than the caller's authorized set.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .mongo_models import ActiveScope

from ..rag_service import Answer, ask as rag_ask


def run(
    db: Session,
    question: str,
    active_scope: ActiveScope,
    allowed_ids: set[int] | None,
    user_id: int | None,
    history: list[dict] | None = None,
) -> Answer:
    """Run the Company KB pipeline with the current conversation scope applied.

    Scope → allowed_ids mapping rules
    ----------------------------------
    1. No scope        → pass ``allowed_ids`` as-is (vault-wide search).
    2. Doc scope       → restrict ``allowed_ids`` to scoped document IDs.
       - Exactly 1 doc → also pass ``document_id`` for the scoped fast-path.
    3. Class scope     → restrict ``allowed_ids`` to class member doc IDs.
    4. Combined scope  → intersection of all scoped IDs AND caller's allowed_ids.

    All restrictions are strict sub-sets of the caller's ``allowed_ids``.
    """
    if active_scope.is_empty():
        # Vault-wide search — no restriction beyond caller's RBAC
        return rag_ask(
            db,
            question,
            allowed_ids,
            document_id=None,
            user_id=user_id,
            history=history,
        )

    scoped_ids = active_scope.all_document_ids()

    # Intersect with caller's allowed_ids (RBAC enforcement)
    if allowed_ids is not None:
        effective_ids = scoped_ids & allowed_ids
    else:
        effective_ids = scoped_ids

    if not effective_ids:
        # All scoped documents are outside the user's access — return graceful denial
        from ..rag_service import NO_ANSWER_MESSAGE
        return Answer(mode="notfound", answer=NO_ANSWER_MESSAGE)

    # Single-document fast-path
    doc_only = (
        len(active_scope.documents) == 1
        and not active_scope.classes
        and len(effective_ids) == 1
    )
    document_id = active_scope.documents[0].document_id if doc_only else None

    return rag_ask(
        db,
        question,
        effective_ids,
        document_id=document_id,
        user_id=user_id,
        history=history,
    )
