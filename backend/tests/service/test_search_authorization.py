"""Search/RAG authorization integration (AUTHZ-006)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session


def _view_bundle(db: Session, users: list[object]) -> None:
    from app import models

    permission = models.Permission(code="VIEW")
    db.add(permission)
    db.flush()
    role = models.Role(name=f"shared-view-{uuid4().hex}")
    db.add(role)
    db.flush()
    db.add(models.RolePermission(role_id=role.id, permission_id=permission.id))
    for user in users:
        db.add(
            models.Assignment(
                user_id=user.id,
                role_id=role.id,
                scope_type="GLOBAL",
                scope_id=None,
                effect="ALLOW",
            )
        )
    db.flush()


def _document(db: Session, owner, folder, title: str, body: str):
    from app import models

    document = models.Document(
        folder_id=folder.id,
        title=title,
        content_hash=uuid4().hex,
        status="READY",
        ocr_status="native",
        created_by=owner.id,
    )
    db.add(document)
    db.flush()
    db.add(
        models.DocVersion(
            document_id=document.id,
            version_no=1,
            file_key=f"{document.id}.txt",
            filename=f"{title}.txt",
            content_type="text/plain",
            size=len(body),
            checksum=uuid4().hex,
            ocr_text=body,
            created_by=owner.id,
        )
    )
    db.flush()
    return document


def _rule(
    db: Session,
    *,
    creator,
    permission,
    user=None,
    group=None,
    scope_type: str,
    scope_id: int,
    effect: str = "ALLOW",
):
    from app import models

    value = models.AccessRule(
        principal_type="GROUP" if group is not None else "USER",
        user_id=None if group is not None else user.id,
        group_id=group.id if group is not None else None,
        permission_id=permission.id,
        scope_type=scope_type,
        scope_id=scope_id,
        effect=effect,
        inherits=scope_type != "DOC",
        is_active=True,
        created_by=creator.id,
        reason="search authorization test",
    )
    db.add(value)
    db.flush()
    return value


def _fixture(db: Session, alice, bob):
    from app import models

    _view_bundle(db, [alice, bob])
    permission = db.query(models.Permission).filter_by(code="VIEW").one()
    cabinet = models.Cabinet(name=f"search-cabinet-{uuid4().hex}")
    db.add(cabinet)
    db.flush()
    alice_folder = models.Folder(
        cabinet_id=cabinet.id,
        name=f"alice-folder-{uuid4().hex}",
    )
    bob_folder = models.Folder(
        cabinet_id=cabinet.id,
        name=f"bob-folder-{uuid4().hex}",
    )
    db.add_all([alice_folder, bob_folder])
    db.flush()
    alice_doc = _document(db, alice, alice_folder, "Alice private", "shared query alpha")
    bob_doc = _document(db, bob, bob_folder, "Bob private", "shared query beta")

    group = models.Group(
        name=f"bob-group-{uuid4().hex}",
        description="search test group",
        created_by=alice.id,
    )
    db.add(group)
    db.flush()
    db.add(
        models.GroupMembership(
            group_id=group.id,
            user_id=bob.id,
            created_by=alice.id,
        )
    )
    db.flush()
    _rule(
        db,
        creator=alice,
        permission=permission,
        user=alice,
        scope_type="DOC",
        scope_id=alice_doc.id,
    )
    _rule(
        db,
        creator=alice,
        permission=permission,
        group=group,
        scope_type="FOLDER",
        scope_id=bob_folder.id,
    )
    db.commit()
    from app.repositories import search_repository

    search_repository.index_fts(db, alice_doc.id, alice_doc.title, "shared query alpha")
    search_repository.index_fts(db, bob_doc.id, bob_doc.title, "shared query beta")
    db.commit()
    return alice_doc, bob_doc


def test_same_role_users_search_only_exact_doc_or_group_scope(
    db_session: Session,
    user_factory: Callable[..., object],
) -> None:
    from app.services import search_application_service, search_authorization

    alice = user_factory(username="search-authz-alice")
    bob = user_factory(username="search-authz-bob")
    alice_doc, bob_doc = _fixture(db_session, alice, bob)

    alice_ids = search_authorization.resolve_view_document_ids(db_session, alice)
    bob_ids = search_authorization.resolve_view_document_ids(db_session, bob)
    assert alice_ids == frozenset({alice_doc.id})
    assert bob_ids == frozenset({bob_doc.id})

    alice_result = search_application_service.run_search(
        db_session,
        alice,
        query="shared query",
        allowed_ids=set(alice_ids),
        limit=20,
        mode="keyword",
    )
    bob_result = search_application_service.run_search(
        db_session,
        bob,
        query="shared query",
        allowed_ids=set(bob_ids),
        limit=20,
        mode="keyword",
    )
    assert {hit.document_id for hit in alice_result.hits} == {alice_doc.id}
    assert {hit.document_id for hit in bob_result.hits} == {bob_doc.id}


def test_ask_guessed_document_id_is_denied_without_citation(
    db_session: Session,
    user_factory: Callable[..., object],
) -> None:
    from app.services import search_application_service, search_authorization

    alice = user_factory(username="ask-authz-alice")
    bob = user_factory(username="ask-authz-bob")
    alice_doc, bob_doc = _fixture(db_session, alice, bob)
    alice_ids = search_authorization.resolve_view_document_ids(db_session, alice)

    answer = search_application_service.ask(
        db_session,
        alice,
        question="What is in this file?",
        allowed_ids=set(alice_ids),
        document_id=bob_doc.id,
    )
    assert answer.scoped_document_id is None
    assert answer.citations == []
    assert "access" in answer.answer.lower()
    assert alice_doc.id not in {item["document_id"] for item in answer.citations}


def test_revocation_is_visible_on_next_search_request(
    db_session: Session,
    user_factory: Callable[..., object],
) -> None:
    from app import models
    from app.services import search_application_service, search_authorization

    alice = user_factory(username="revoke-authz-alice")
    bob = user_factory(username="revoke-authz-bob")
    alice_doc, _bob_doc = _fixture(db_session, alice, bob)

    before = search_authorization.resolve_view_document_ids(db_session, alice)
    first = search_application_service.run_search(
        db_session,
        alice,
        query="alpha",
        allowed_ids=set(before),
        limit=20,
        mode="keyword",
    )
    assert [hit.document_id for hit in first.hits] == [alice_doc.id]

    rule = (
        db_session.query(models.AccessRule)
        .filter_by(user_id=alice.id, scope_type="DOC", scope_id=alice_doc.id)
        .one()
    )
    rule.is_active = False
    db_session.commit()

    after = search_authorization.resolve_view_document_ids(
        db_session,
        alice,
        now=datetime.now(UTC),
    )
    second = search_application_service.run_search(
        db_session,
        alice,
        query="alpha",
        allowed_ids=set(after),
        limit=20,
        mode="keyword",
    )
    assert after == frozenset()
    assert second.hits == []

