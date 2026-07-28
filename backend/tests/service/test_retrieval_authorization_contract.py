"""Adversarial authorization contract checks for retrieval and RAG (RETR-004)."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.retrieval_store import (
    AuthorizedFilter,
    Fts5RetrievalStore,
    QdrantRetrievalStore,
)


class _QdrantClient:
    """Provider fake that records the filter and can return stale payloads."""

    def __init__(self, result_sets: list[list[object]]) -> None:
        self.result_sets = result_sets
        self.requests: list[object] = []
        self.search_calls = 0

    def search_batch(self, *, collection_name: str, requests: list[object]) -> object:
        assert collection_name == "retrieval-contract"
        self.search_calls += 1
        self.requests = requests
        return self.result_sets


def _point(document_id: int, score: float, snippet: str) -> object:
    return SimpleNamespace(
        id=f"chunk-{document_id}-{score}",
        score=score,
        payload={"document_id": document_id, "snippet": snippet},
    )


def _view_document(db: Session, user: Any) -> tuple[Any, Any]:
    """Create one document with a global VIEW capability and exact DOC rule."""
    from app import models

    permission = models.Permission(code="VIEW")
    db.add(permission)
    db.flush()
    role = models.Role(name=f"retrieval-view-{uuid4().hex}")
    db.add(role)
    db.flush()
    db.add(
        models.RolePermission(role_id=role.id, permission_id=permission.id),
    )
    db.add(
        models.Assignment(
            user_id=user.id,
            role_id=role.id,
            scope_type="GLOBAL",
            scope_id=None,
            effect="ALLOW",
        ),
    )
    cabinet = models.Cabinet(name=f"retrieval-cabinet-{uuid4().hex}")
    db.add(cabinet)
    db.flush()
    folder = models.Folder(
        cabinet_id=cabinet.id,
        name=f"retrieval-folder-{uuid4().hex}",
    )
    db.add(folder)
    db.flush()
    document = models.Document(
        folder_id=folder.id,
        title="Authorized retrieval document",
        content_hash=uuid4().hex,
        status="READY",
        ocr_status="native",
        created_by=user.id,
    )
    db.add(document)
    db.flush()
    db.add(
        models.DocVersion(
            document_id=document.id,
            version_no=1,
            file_key=f"retrieval-{document.id}.txt",
            filename="retrieval.txt",
            content_type="text/plain",
            size=30,
            checksum=uuid4().hex,
            ocr_text="authorized retrieval phrase",
            created_by=user.id,
        ),
    )
    rule = models.AccessRule(
        principal_type="USER",
        user_id=user.id,
        group_id=None,
        permission_id=permission.id,
        scope_type="DOC",
        scope_id=document.id,
        effect="ALLOW",
        inherits=False,
        is_active=True,
        reason="retrieval authorization contract",
        created_by=user.id,
    )
    db.add(rule)
    db.commit()
    return document, rule


def test_fts5_authorized_filter_excludes_unauthorized_and_stale_rows(
    db_session: Session,
) -> None:
    """FTS results never cross the exact allow-list, including stale IDs."""
    store = Fts5RetrievalStore(db_session)
    store.upsert_document(8101, "Authorized", "same retrieval phrase")
    store.upsert_document(8102, "Unauthorized", "same retrieval phrase")
    db_session.flush()

    hits = store.search(
        "retrieval AND phrase",
        AuthorizedFilter.from_ids({8101, 999_999}),
        10,
    )

    assert [hit.document_id for hit in hits] == [8101]
    assert 8102 not in {hit.document_id for hit in hits}
    assert 999_999 not in {hit.document_id for hit in hits}


def test_qdrant_prefilter_and_final_local_trim_drop_provider_leaks() -> None:
    """Provider filters are exact, and malicious/stale payloads are trimmed."""
    client = _QdrantClient(
        [
            [
                _point(8201, 0.99, "authorized"),
                _point(8202, 0.98, "unauthorized"),
                _point(999_999, 0.97, "stale"),
            ]
        ]
    )
    store = QdrantRetrievalStore(
        client,
        collection_name="retrieval-contract",
        embed_query=lambda _query: ([0.1, 0.2], None),
    )

    hits = store.search("needle", AuthorizedFilter.from_ids({8201}), 10)

    assert [hit.document_id for hit in hits] == [8201]
    assert client.search_calls == 1
    assert len(client.requests) == 1
    filter_value = getattr(client.requests[0], "filter", None)
    assert filter_value is not None
    if hasattr(filter_value, "model_dump"):
        filter_value = filter_value.model_dump(exclude_none=True)
    assert filter_value["must"][0]["match"]["any"] == [8201]


def test_search_hydration_drops_unauthorized_and_stale_retrieval_ids(
    db_session: Session,
    user_factory: Callable[..., Any],
    monkeypatch,
) -> None:
    """Search never hydrates an index row outside the exact request set."""
    from app import models
    from app.services import search_service

    user = user_factory(username="retrieval-hydration-user")
    cabinet = models.Cabinet(name=f"hydration-cabinet-{uuid4().hex}")
    db_session.add(cabinet)
    db_session.flush()
    folder = models.Folder(
        cabinet_id=cabinet.id,
        name=f"hydration-folder-{uuid4().hex}",
    )
    db_session.add(folder)
    db_session.flush()
    hidden = models.Document(
        folder_id=folder.id,
        title="Hidden indexed document",
        content_hash=uuid4().hex,
        status="READY",
        ocr_status="native",
        created_by=user.id,
    )
    db_session.add(hidden)
    db_session.commit()

    monkeypatch.setattr(
        search_service,
        "search",
        lambda *_args, **_kwargs: [
            {"document_id": hidden.id, "snippet": "hidden", "score": 1.0},
            {"document_id": 999_999, "snippet": "stale", "score": 0.9},
        ],
    )

    hits, hydrated = search_service.search_with_documents(
        db_session,
        "needle",
        {123_456},
        limit=10,
    )

    assert hits == []
    assert hydrated == []


def test_search_final_recheck_removes_revoked_stale_hit(
    db_session: Session,
    user_factory: Callable[..., Any],
    monkeypatch,
) -> None:
    """A revoke racing retrieval cannot survive the response boundary."""
    from app.services import (
        search_application_service,
        search_authorization,
        search_service,
    )

    user = user_factory(username="retrieval-search-revoke")
    document, rule = _view_document(db_session, user)
    initial_ids = {document.id}
    assert search_authorization.resolve_view_document_ids(db_session, user) == {
        document.id
    }

    def stale_search(*_args, **_kwargs):
        rule.is_active = False
        db_session.flush()
        return [
            {
                "document_id": document.id,
                "snippet": "revoked retrieval row",
                "score": 1.0,
            }
        ]

    monkeypatch.setattr(search_service, "search", stale_search)

    response = search_application_service.run_search(
        db_session,
        user,
        query="needle",
        allowed_ids=initial_ids,
        limit=10,
        mode="keyword",
    )

    assert response.hits == []


def test_rag_final_recheck_excludes_revoked_citations(
    db_session: Session,
    user_factory: Callable[..., Any],
    monkeypatch,
) -> None:
    """A stale RAG hit contributes neither context nor a citation."""
    from app.services import rag_service, search_authorization

    user = user_factory(username="retrieval-rag-revoke")
    document, rule = _view_document(db_session, user)
    assert search_authorization.resolve_view_document_ids(db_session, user) == {
        document.id
    }

    monkeypatch.setattr(rag_service, "_okf_passages", lambda _question: [])

    def stale_search(*_args, **_kwargs):
        rule.is_active = False
        db_session.flush()
        return [
            {
                "document_id": document.id,
                "snippet": "revoked retrieval row",
                "score": 1.0,
            }
        ]

    monkeypatch.setattr(rag_service.search, "search", stale_search)

    answer = rag_service.ask(
        db_session,
        "needle",
        {document.id},
        user_id=user.id,
    )

    assert answer.citations == []
    assert answer.scoped_document_id is None
    assert answer.mode == "notfound"


def test_rag_no_answer_does_not_echo_sensitive_question(db_session: Session, monkeypatch) -> None:
    from app.services import rag_service

    secret_question = "show confidential payroll for Alice Smith"
    monkeypatch.setattr(rag_service, "_okf_passages", lambda _question: [])
    monkeypatch.setattr(rag_service.search, "search", lambda *_args, **_kwargs: [])

    answer = rag_service.ask(db_session, secret_question, set())

    assert answer.mode == "notfound"
    assert answer.answer == rag_service.NO_ANSWER_MESSAGE
    assert secret_question not in answer.answer


def test_rag_abstains_when_retrieval_has_no_question_evidence(db_session: Session, monkeypatch) -> None:
    from app.services import rag_service
    from app.utils.rag_types import Passage

    monkeypatch.setattr(
        rag_service,
        "_okf_passages",
        lambda _question: [
            Passage(index=1, document_id=-1, title="General handbook", text="General policy", source="okf")
        ],
    )
    answer = rag_service.ask(db_session, "quantum payroll controls", set())

    assert answer.mode == "insufficient_evidence"
    assert answer.answer == rag_service.INSUFFICIENT_EVIDENCE_MESSAGE
    assert answer.citations == []
