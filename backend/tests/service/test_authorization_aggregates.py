"""Authorization-aware list, dashboard, and duplicate aggregates (AUTHZ-007)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session


def _permission(db: Session, code: str):
    from app import models

    value = db.query(models.Permission).filter_by(code=code).one_or_none()
    if value is None:
        value = models.Permission(code=code)
        db.add(value)
        db.flush()
    return value


def _capabilities(db: Session, user, *codes: str) -> None:
    from app import models

    role = models.Role(name=f"aggregate-cap-{uuid4().hex}")
    db.add(role)
    db.flush()
    for code in codes:
        db.add(
            models.RolePermission(
                role_id=role.id,
                permission_id=_permission(db, code).id,
            )
        )
    db.add(
        models.Assignment(
            user_id=user.id,
            role_id=role.id,
            scope_type="GLOBAL",
            effect="ALLOW",
        )
    )
    db.flush()


def _document(db: Session, creator, *, title: str):
    from app import models

    cabinet = db.query(models.Cabinet).first()
    if cabinet is None:
        cabinet = models.Cabinet(name=f"aggregate-cabinet-{uuid4().hex}")
        db.add(cabinet)
        db.flush()
    folder = db.query(models.Folder).filter_by(cabinet_id=cabinet.id).first()
    if folder is None:
        folder = models.Folder(
            cabinet_id=cabinet.id,
            name=f"aggregate-folder-{uuid4().hex}",
        )
        db.add(folder)
        db.flush()
    value = models.Document(
        folder_id=folder.id,
        title=title,
        content_hash=uuid4().hex,
        created_by=creator.id,
        status="READY",
        ocr_status="native",
    )
    db.add(value)
    db.flush()
    return value


def _rule(
    db: Session,
    *,
    creator,
    principal,
    permission: str,
    document_id: int,
) -> None:
    from app import models

    value = models.AccessRule(
        principal_type="USER",
        user_id=principal.id,
        permission_id=_permission(db, permission).id,
        scope_type="DOC",
        scope_id=document_id,
        effect="ALLOW",
        inherits=False,
        is_active=True,
        reason="aggregate visibility test",
        created_by=creator.id,
    )
    db.add(value)
    db.flush()


def _visible(db: Session, user, permission: str = "VIEW") -> frozenset[int]:
    from app.repositories.visible_document_repository import (
        resolve_visible_document_ids,
    )
    from app.services.search_authorization import resolve_view_document_ids

    if permission == "VIEW":
        return resolve_view_document_ids(db, user)
    return resolve_visible_document_ids(
        db,
        user_id=user.id,
        permission=permission,
        now=datetime.now(UTC),
    )


def test_list_dashboard_and_duplicate_members_are_scoped_without_id_leaks(
    db_session: Session,
    user_factory: Callable[..., object],
) -> None:
    from app import models
    from app.repositories import admin_repository
    from app.services import document_service, duplicate_service

    alice = user_factory(username="aggregate-alice")
    bob = user_factory(username="aggregate-bob")
    _capabilities(db_session, alice, "VIEW", "DELETE")
    _capabilities(db_session, bob, "VIEW", "DELETE")
    primary = _document(db_session, alice, title="visible-primary")
    hidden = _document(db_session, alice, title="hidden-duplicate")
    _rule(
        db_session,
        creator=alice,
        principal=alice,
        permission="VIEW",
        document_id=primary.id,
    )
    _rule(
        db_session,
        creator=alice,
        principal=alice,
        permission="VIEW",
        document_id=hidden.id,
    )
    _rule(
        db_session,
        creator=alice,
        principal=bob,
        permission="VIEW",
        document_id=primary.id,
    )
    for principal in (alice, bob):
        _rule(
            db_session,
            creator=alice,
            principal=principal,
            permission="DELETE",
            document_id=primary.id,
        )
    _rule(
        db_session,
        creator=alice,
        principal=alice,
        permission="DELETE",
        document_id=hidden.id,
    )
    group = models.DupGroup(
        primary_document_id=primary.id,
        similarity_type="exact",
        resolved=False,
    )
    db_session.add(group)
    db_session.flush()
    db_session.add_all(
        [
            models.DupMember(
                dup_group_id=group.id,
                document_id=primary.id,
                similarity_score=1.0,
            ),
            models.DupMember(
                dup_group_id=group.id,
                document_id=hidden.id,
                similarity_score=1.0,
            ),
        ]
    )
    db_session.flush()

    alice_ids = _visible(db_session, alice)
    bob_ids = _visible(db_session, bob)
    assert alice_ids == {primary.id, hidden.id}
    assert bob_ids == {primary.id}
    assert [
        item.id
        for item in document_service.list_documents(
            db_session,
            allowed_ids=set(bob_ids),
            limit=100,
        )
    ] == [primary.id]

    bob_groups = duplicate_service.list_groups(
        db_session,
        include_resolved=False,
        user=bob,
    )
    assert len(bob_groups) == 1
    assert bob_groups[0].primary_document_id == primary.id
    assert [member.document_id for member in bob_groups[0].members] == [primary.id]
    assert hidden.id not in {member.document_id for member in bob_groups[0].members}

    bob_counts = admin_repository.dashboard_counts(
        db_session,
        visible_document_ids=bob_ids,
    )
    assert bob_counts["total_documents"] == 1
    assert bob_counts["duplicate_members"] == 1
    assert bob_counts["open_duplicate_groups"] == 1


def test_duplicate_resolution_requires_every_member_delete_capability(
    db_session: Session,
    user_factory: Callable[..., object],
) -> None:
    from app import models, schemas
    from app.services import duplicate_service
    from app.services.exceptions import NotFoundError

    alice = user_factory(username="resolve-alice")
    bob = user_factory(username="resolve-bob")
    _capabilities(db_session, alice, "VIEW", "DELETE")
    _capabilities(db_session, bob, "VIEW", "DELETE")
    primary = _document(db_session, alice, title="resolve-primary")
    hidden = _document(db_session, alice, title="resolve-hidden")
    for document in (primary, hidden):
        for permission in ("VIEW", "DELETE"):
            _rule(
                db_session,
                creator=alice,
                principal=alice,
                permission=permission,
                document_id=document.id,
            )
    for permission in ("VIEW", "DELETE"):
        _rule(
            db_session,
            creator=alice,
            principal=bob,
            permission=permission,
            document_id=primary.id,
        )
    group = models.DupGroup(primary_document_id=primary.id, similarity_type="exact")
    db_session.add(group)
    db_session.flush()
    db_session.add_all(
        [
            models.DupMember(dup_group_id=group.id, document_id=primary.id),
            models.DupMember(dup_group_id=group.id, document_id=hidden.id),
        ]
    )
    db_session.flush()

    with pytest.raises(NotFoundError, match="Duplicate group not found"):
        duplicate_service.resolve_group(
            db_session,
            bob,
            group.id,
            schemas.ResolveDup(
                primary_document_id=primary.id,
                action="keep_both",
            ),
        )
    with pytest.raises(NotFoundError, match="Duplicate group not found"):
        duplicate_service.resolve_group(
            db_session,
            bob,
            999_999,
            schemas.ResolveDup(
                primary_document_id=primary.id,
                action="keep_both",
            ),
        )
