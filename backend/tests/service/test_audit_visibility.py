"""AUTHZ-008 audit visibility tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app import models


def _permission(db: Session, code: str):
    from app import models

    permission = db.query(models.Permission).filter_by(code=code).one_or_none()
    if permission is None:
        permission = models.Permission(code=code)
        db.add(permission)
        db.flush()
    return permission


def _role(db: Session, *codes: str):
    from app import models

    role = models.Role(name=f"audit-role-{uuid4().hex}")
    db.add(role)
    db.flush()
    for code in codes:
        db.add(
            models.RolePermission(
                role_id=role.id,
                permission_id=_permission(db, code).id,
            )
        )
    db.flush()
    return role


def _assignment(
    db: Session,
    user,
    role,
    *,
    scope_type: str,
    scope_id: int | None,
) -> None:
    from app import models

    db.add(
        models.Assignment(
            user_id=user.id,
            role_id=role.id,
            scope_type=scope_type,
            scope_id=scope_id,
            effect="ALLOW",
        )
    )
    db.flush()


def _document(db: Session, creator):
    from app import models

    cabinet = models.Cabinet(name=f"audit-cabinet-{uuid4().hex}")
    db.add(cabinet)
    db.flush()
    folder = models.Folder(cabinet_id=cabinet.id, name=f"audit-folder-{uuid4().hex}")
    db.add(folder)
    db.flush()
    document = models.Document(
        folder_id=folder.id,
        title=f"audit-document-{uuid4().hex}",
        content_hash=uuid4().hex,
        created_by=creator.id,
    )
    db.add(document)
    db.flush()
    return document


def _view_rule(db: Session, *, user, creator, document_id: int) -> None:
    from app import models

    db.add(
        models.AccessRule(
            principal_type="USER",
            user_id=user.id,
            group_id=None,
            permission_id=_permission(db, "VIEW").id,
            scope_type="DOC",
            scope_id=document_id,
            effect="ALLOW",
            inherits=False,
            is_active=True,
            expires_at=None,
            reason="audit visibility test",
            created_by=creator.id,
        )
    )
    db.flush()


def _audit(db: Session, *, object_type: str, object_id: str, details: str = "{}"):
    from app import models

    entry = models.AuditLog(
        actor_name="actor",
        action="TEST_AUDIT",
        object_type=object_type,
        object_id=object_id,
        details=details,
    )
    db.add(entry)
    db.flush()
    return entry


def _list(db: Session, user, *, object_id: str | None = None):
    from app.services import audit_service

    return audit_service.list_entries(
        db,
        action=None,
        actor=None,
        object_id=object_id,
        limit=100,
        user=user,
    )


def test_global_auditor_keeps_bounded_audit_filters(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    user = user_factory(username="global-auditor")
    role = _role(db_session, "VIEW_AUDIT")
    _assignment(db_session, user, role, scope_type="GLOBAL", scope_id=None)
    document = _document(db_session, user)
    _audit(db_session, object_type="document", object_id=str(document.id))
    _audit(db_session, object_type="folder", object_id="77")
    _audit(db_session, object_type="user", object_id="88")

    rows = _list(db_session, user)

    assert {row.object_type for row in rows} == {"document", "folder", "user"}


def test_resource_scoped_auditors_with_same_role_see_only_their_view_ids(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    alice = user_factory(username="scoped-auditor-alice")
    bob = user_factory(username="scoped-auditor-bob")
    role = _role(db_session, "VIEW", "VIEW_AUDIT")
    document_a = _document(db_session, alice)
    document_b = _document(db_session, bob)
    _assignment(
        db_session,
        alice,
        role,
        scope_type="DOC",
        scope_id=document_a.id,
    )
    _assignment(
        db_session,
        bob,
        role,
        scope_type="DOC",
        scope_id=document_b.id,
    )
    _view_rule(db_session, user=alice, creator=alice, document_id=document_a.id)
    _view_rule(db_session, user=bob, creator=bob, document_id=document_b.id)
    _audit(
        db_session,
        object_type="document",
        object_id=str(document_a.id),
        details='{"hits":2,"duplicate_of":999999,"private":"must disappear"}',
    )
    _audit(db_session, object_type="document", object_id=str(document_b.id))
    _audit(db_session, object_type="folder", object_id="999")

    alice_rows = _list(db_session, alice)
    bob_rows = _list(db_session, bob)

    assert {row.object_id for row in alice_rows} == {str(document_a.id)}
    assert {row.object_id for row in bob_rows} == {str(document_b.id)}
    assert "999999" not in alice_rows[0].details
    assert "private" not in alice_rows[0].details
    assert _list(db_session, alice, object_id=str(document_b.id)) == []


def test_scoped_audit_empty_or_guessed_ids_fail_closed(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    user = user_factory(username="scoped-auditor-empty")
    role = _role(db_session, "VIEW", "VIEW_AUDIT")
    _assignment(db_session, user, role, scope_type="FOLDER", scope_id=999)
    _audit(
        db_session,
        object_type="document",
        object_id="424242",
        details='{"private":"unauthorized"}',
    )
    _audit(db_session, object_type="user", object_id="1")

    assert _list(db_session, user) == []
    assert _list(db_session, user, object_id="424242") == []
