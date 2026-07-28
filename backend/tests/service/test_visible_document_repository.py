"""Exact, bounded visible-document-ID resolution tests (AUTHZ-004)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app import models


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _permission(db: Session, code: str):
    from app import models

    value = db.query(models.Permission).filter_by(code=code).one_or_none()
    if value is None:
        value = models.Permission(code=code)
        db.add(value)
        db.flush()
    return value


def _grant_view(db: Session, user) -> None:
    from app import models

    role = models.Role(name=f"view-bundle-{uuid4().hex}")
    db.add(role)
    db.flush()
    db.add(
        models.RolePermission(role_id=role.id, permission_id=_permission(db, "VIEW").id)
    )
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


def _hierarchy(db: Session, creator, *, child_cabinet: bool = False):
    from app import models

    cabinet = models.Cabinet(name=f"cabinet-{uuid4().hex}")
    db.add(cabinet)
    db.flush()
    folder = models.Folder(
        cabinet_id=cabinet.id,
        name=f"folder-{uuid4().hex}",
    )
    db.add(folder)
    db.flush()
    other_cabinet = None
    if child_cabinet:
        other_cabinet = models.Cabinet(name=f"other-cabinet-{uuid4().hex}")
        db.add(other_cabinet)
        db.flush()
    documents = []
    for index in range(2):
        document = models.Document(
            folder_id=folder.id,
            title=f"private-{index}-{uuid4().hex}",
            content_hash=uuid4().hex,
            created_by=creator.id,
        )
        db.add(document)
        db.flush()
        documents.append(document)
    return cabinet, folder, documents, other_cabinet


def _rule(
    db: Session,
    *,
    creator,
    principal,
    scope_type: str,
    scope_id: int | None,
    effect: str = "ALLOW",
    inherits: bool = True,
    expires_at=None,
):
    from app import models

    is_group = isinstance(principal, models.Group)
    value = models.AccessRule(
        principal_type="GROUP" if is_group else "USER",
        user_id=None if is_group else principal.id,
        group_id=principal.id if is_group else None,
        permission_id=_permission(db, "VIEW").id,
        scope_type=scope_type,
        scope_id=scope_id,
        effect=effect,
        inherits=inherits,
        is_active=True,
        expires_at=expires_at,
        reason="bounded test rule",
        created_by=creator.id,
    )
    db.add(value)
    db.flush()
    return value


def _resolve(db: Session, user, *, now: datetime = NOW, limits=None):
    from app.repositories.visible_document_repository import (
        resolve_visible_document_ids,
    )

    kwargs = {
        "db": db,
        "user_id": user.id,
        "permission": "VIEW",
        "now": now,
    }
    if limits is not None:
        kwargs["limits"] = limits
    return resolve_visible_document_ids(**kwargs)


@contextmanager
def _capture_sql(db: Session) -> Iterator[list[str]]:
    statements: list[str] = []
    bind = db.get_bind()

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(bind, "before_cursor_execute", capture)


def test_same_role_users_have_different_exact_visible_ids(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    alice = user_factory(username="authz-alice")
    bob = user_factory(username="authz-bob")
    _grant_view(db_session, alice)
    _grant_view(db_session, bob)
    cabinet, folder, documents, _ = _hierarchy(db_session, alice)
    _rule(
        db_session,
        creator=alice,
        principal=alice,
        scope_type="DOC",
        scope_id=documents[0].id,
        inherits=False,
    )

    assert _resolve(db_session, alice) == frozenset({documents[0].id})
    assert _resolve(db_session, bob) == frozenset()
    assert 999999 not in _resolve(db_session, alice)


def test_capability_and_active_user_gates_default_deny(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    inactive = user_factory(username="authz-inactive", status="suspended")
    no_capability = user_factory(username="authz-no-capability")
    _grant_view(db_session, inactive)
    cabinet, folder, documents, _ = _hierarchy(db_session, inactive)
    _rule(
        db_session,
        creator=inactive,
        principal=inactive,
        scope_type="FOLDER",
        scope_id=folder.id,
        inherits=True,
    )
    _rule(
        db_session,
        creator=inactive,
        principal=no_capability,
        scope_type="FOLDER",
        scope_id=folder.id,
        inherits=True,
    )

    assert _resolve(db_session, inactive) == frozenset()
    assert _resolve(db_session, no_capability) == frozenset()
    assert all(
        document.id not in _resolve(db_session, no_capability)
        for document in documents
    )


def test_group_allow_deny_direct_exception_and_active_membership(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    user = user_factory(username="authz-group-user")
    creator = user_factory(username="authz-group-creator")
    _grant_view(db_session, user)
    cabinet, folder, documents, _ = _hierarchy(db_session, creator)
    from app import models

    group = models.Group(
        name=f"active-group-{uuid4().hex}",
        description="group",
        is_active=True,
        created_by=creator.id,
    )
    inactive = models.Group(
        name=f"inactive-group-{uuid4().hex}",
        description="inactive",
        is_active=False,
        created_by=creator.id,
    )
    db_session.add_all([group, inactive])
    db_session.flush()
    db_session.add_all(
        [
            models.GroupMembership(
                group_id=group.id,
                user_id=user.id,
                created_by=creator.id,
            ),
            models.GroupMembership(
                group_id=inactive.id,
                user_id=user.id,
                created_by=creator.id,
            ),
        ]
    )
    db_session.flush()
    _rule(
        db_session,
        creator=creator,
        principal=group,
        scope_type="FOLDER",
        scope_id=folder.id,
        inherits=True,
    )
    _rule(
        db_session,
        creator=creator,
        principal=group,
        scope_type="DOC",
        scope_id=documents[0].id,
        effect="DENY",
        inherits=False,
    )
    _rule(
        db_session,
        creator=creator,
        principal=inactive,
        scope_type="DOC",
        scope_id=documents[1].id,
        inherits=False,
    )

    assert _resolve(db_session, user) == frozenset({documents[1].id})

    # A direct USER DOC allow outranks the GROUP DOC deny at the same scope;
    # then a direct USER deny can revoke the inherited/group result.
    _rule(
        db_session,
        creator=creator,
        principal=user,
        scope_type="DOC",
        scope_id=documents[0].id,
        effect="ALLOW",
        inherits=False,
    )
    assert _resolve(db_session, user) == frozenset({documents[0].id, documents[1].id})
    _rule(
        db_session,
        creator=creator,
        principal=user,
        scope_type="DOC",
        scope_id=documents[1].id,
        effect="DENY",
        inherits=False,
    )
    assert _resolve(db_session, user) == frozenset({documents[0].id})


def test_cabinet_and_global_inheritance_expiry_and_noninheritance(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    user = user_factory(username="authz-inheritance")
    _grant_view(db_session, user)
    cabinet, folder, documents, _ = _hierarchy(db_session, user)
    _rule(
        db_session,
        creator=user,
        principal=user,
        scope_type="CABINET",
        scope_id=cabinet.id,
        inherits=True,
    )
    assert _resolve(db_session, user) == frozenset({d.id for d in documents})

    from app import models

    db_session.query(models.AccessRule).delete(synchronize_session=False)
    db_session.flush()
    _rule(
        db_session,
        creator=user,
        principal=user,
        scope_type="GLOBAL",
        scope_id=None,
        inherits=False,
    )
    assert _resolve(db_session, user) == frozenset()
    _rule(
        db_session,
        creator=user,
        principal=user,
        scope_type="FOLDER",
        scope_id=folder.id,
        inherits=True,
        expires_at=NOW + timedelta(seconds=1),
    )
    assert _resolve(db_session, user, now=NOW) == frozenset({d.id for d in documents})
    assert _resolve(db_session, user, now=NOW + timedelta(seconds=1)) == frozenset()


def test_malformed_missing_and_cyclic_hierarchies_fail_closed(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    user = user_factory(username="authz-malformed")
    _grant_view(db_session, user)
    cabinet, folder, documents, _ = _hierarchy(db_session, user)
    _rule(
        db_session,
        creator=user,
        principal=user,
        scope_type="GLOBAL",
        scope_id=None,
        inherits=True,
    )
    # A dangling parent and a cycle must not be treated as a shorter valid path.
    db_session.commit()
    db_session.execute(text("PRAGMA foreign_keys=OFF"))
    db_session.execute(
        text("UPDATE folders SET parent_id = :missing WHERE id = :folder_id"),
        {"missing": 987654, "folder_id": folder.id},
    )
    db_session.commit()
    db_session.execute(text("PRAGMA foreign_keys=ON"))
    assert _resolve(db_session, user) == frozenset()

    db_session.execute(
        text("UPDATE folders SET parent_id = :folder_id WHERE id = :folder_id"),
        {"folder_id": folder.id},
    )
    db_session.commit()
    assert _resolve(db_session, user) == frozenset()
    assert cabinet.id not in _resolve(db_session, user)
    assert all(document.id not in _resolve(db_session, user) for document in documents)


def test_resolution_uses_one_set_based_query_and_never_truncates(
    db_session: Session,
    user_factory: Callable[..., models.User],
) -> None:
    from app.repositories.visible_document_repository import VisibleDocumentLimits

    user = user_factory(username="authz-bounded")
    _grant_view(db_session, user)
    cabinet, folder, documents, _ = _hierarchy(db_session, user)
    _rule(
        db_session,
        creator=user,
        principal=user,
        scope_type="FOLDER",
        scope_id=folder.id,
        inherits=True,
    )
    with _capture_sql(db_session) as statements:
        visible = _resolve(db_session, user)
    assert visible == frozenset({d.id for d in documents})
    assert visible.issubset({d.id for d in documents})
    recursive = [statement for statement in statements if "WITH RECURSIVE" in statement]
    assert len(recursive) == 1
    assert len(statements) <= 4

    tiny = VisibleDocumentLimits(max_document_ids=1)
    try:
        _resolve(db_session, user, limits=tiny)
    except Exception as exc:
        assert exc.__class__.__name__ == "VisibleDocumentResolutionUnavailable"
    else:
        raise AssertionError("visible IDs must not be silently truncated")

    # Rule overflow is also fail-closed: the query never evaluates a truncated
    # prefix of ACL rows as if it were the complete policy.
    _rule(
        db_session,
        creator=user,
        principal=user,
        scope_type="DOC",
        scope_id=documents[0].id,
        inherits=False,
    )
    assert _resolve(
        db_session,
        user,
        limits=VisibleDocumentLimits(max_rules=1),
    ) == frozenset()
