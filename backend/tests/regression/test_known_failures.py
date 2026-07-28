"""Deterministic reproductions for the P0/P1 defects captured by TEST-003.

Each test states the required safe behavior and is a strict expected failure
until its owning work package implements that behavior. Run this file with
``pytest --runxfail`` to prove that the current implementation fails all of the
security/reliability assertions. Once a defect is fixed, strict XPASS turns the
normal suite red until the marker is removed.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest
from sqlalchemy.orm import Session


@pytest.mark.xfail(
    strict=True,
    reason="PR-01: assignment scope is collapsed into a global VIEW permission",
)
def test_document_scoped_view_does_not_expand_to_every_document(
    db_session: Session,
    user_factory,
) -> None:
    from app import models
    from app.services import rbac_service

    user = user_factory(
        username="doc-scoped-viewer",
        email="doc-scoped-viewer@example.test",
    )
    permission = models.Permission(code="VIEW")
    role = models.Role(name="Document Viewer", description="test-only role")
    db_session.add_all([permission, role])
    db_session.flush()
    db_session.add(
        models.RolePermission(
            role_id=role.id,
            permission_id=permission.id,
        )
    )
    db_session.add(
        models.Assignment(
            user_id=user.id,
            role_id=role.id,
            scope_type="DOC",
            scope_id=4242,
            effect="ALLOW",
        )
    )
    db_session.flush()

    assert rbac_service.viewable_document_ids(db_session, user) == {4242}


def test_folder_import_rejects_an_unapproved_server_path(
    db_session: Session,
    user_factory,
    test_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models, schemas
    from app.services import document_service
    from app.services.exceptions import ServiceError

    user = user_factory(
        username="folder-importer",
        email="folder-importer@example.test",
    )
    cabinet = models.Cabinet(name="Test Cabinet")
    db_session.add(cabinet)
    db_session.flush()
    folder = models.Folder(cabinet_id=cabinet.id, name="Test Folder")
    db_session.add(folder)
    db_session.flush()

    unapproved = test_paths.root / "unapproved-server-directory"
    unapproved.mkdir()
    (unapproved / "private.txt").write_text(
        "server-local content must not be importable by an API caller",
        encoding="utf-8",
    )
    approved = test_paths.root / "approved-server-directory"
    approved.mkdir()
    monkeypatch.setattr(document_service.settings, "folder_import_enabled", True)
    monkeypatch.setattr(
        document_service.settings,
        "folder_import_roots",
        [approved],
    )

    with pytest.raises(ServiceError):
        document_service.import_folder(
            db_session,
            user,
            schemas.ImportRequest(
                path=str(unapproved),
                recursive=False,
                folder_id=folder.id,
            ),
        )


def test_default_provider_cannot_probe_or_send_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    settings_env: dict[str, str],
) -> None:
    from app.config import Settings
    from app.services import rag_service
    from app.utils.rag_types import Passage

    # Read code defaults, excluding both the test environment and any local .env.
    monkeypatch.delenv("DOCVAULT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DOCVAULT_VLLM_URL", raising=False)
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]

    outbound: list[tuple[str, object]] = []

    def send_to_vllm(question: str, passages: list[Passage]) -> str:
        outbound.append(("send", [passage.text for passage in passages]))
        return "external response"

    monkeypatch.setattr(rag_service.settings, "llm_provider", defaults.llm_provider)
    monkeypatch.setattr(rag_service.settings, "vllm_url", defaults.vllm_url)
    monkeypatch.setattr(rag_service, "_answer_with_vllm", send_to_vllm)

    result = rag_service._compose(
        "Summarize the private document",
        [
            Passage(
                index=1,
                document_id=7,
                title="Private",
                text="confidential document excerpt",
            )
        ],
        scoped_id=7,
    )

    assert (defaults.llm_provider, outbound, result.mode) == (
        "none",
        [],
        "extractive",
    )


@pytest.mark.xfail(
    strict=True,
    reason="PR-06: vector upsert exceptions are swallowed without retry or signal",
)
def test_vector_upsert_failure_is_visible(
    monkeypatch: pytest.MonkeyPatch,
    settings_env: dict[str, str],
) -> None:
    from app.repositories import search_repository

    qdrant_package = ModuleType("qdrant_client")
    qdrant_package.__path__ = []
    qdrant_models = ModuleType("qdrant_client.models")

    class PointStruct:
        def __init__(self, **values):
            self.values = values

    class SparseVector:
        def __init__(self, **values):
            self.values = values

    qdrant_models.PointStruct = PointStruct  # type: ignore[attr-defined]
    qdrant_models.SparseVector = SparseVector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_package)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", qdrant_models)

    class BrokenClient:
        def upsert(self, **_kwargs) -> None:
            raise RuntimeError("vector store unavailable")

    monkeypatch.setattr(search_repository, "get_qdrant", lambda: BrokenClient())

    with pytest.raises(RuntimeError, match="vector store unavailable"):
        search_repository.index_qdrant(
            document_id=12,
            title="Vector failure",
            snippet="content",
            dense_vector=[0.1, 0.2],
            sparse_vector=None,
        )


def test_audit_record_does_not_commit_independently(
    db_session: Session,
    user_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models
    from app.services import audit_service

    user = user_factory(
        username="audit-actor",
        email="audit-actor@example.test",
    )

    def fail_commit() -> None:
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(db_session, "commit", fail_commit)
    audit_service.record(
        db_session,
        actor=user,
        action="TEST_AUDIT_FAILURE",
        object_type="test",
        object_id="1",
    )
    # The action owner, not the audit helper, controls the commit boundary.
    db_session.flush()
    assert db_session.query(models.AuditLog).filter_by(action="TEST_AUDIT_FAILURE").one()
