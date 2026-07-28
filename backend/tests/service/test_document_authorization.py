"""Direct-object authorization for document and import targets (AUTHZ-005)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
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


def _capabilities(db: Session, user, codes: Iterable[str]) -> None:
    from app import models

    role = models.Role(name=f"opaque-document-cap-{uuid4().hex}")
    db.add(role)
    db.flush()
    for code in sorted(set(codes)):
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
            scope_id=None,
            effect="ALLOW",
        )
    )
    db.flush()


def _hierarchy(db: Session, creator):
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
    document = models.Document(
        folder_id=folder.id,
        title="restricted.txt",
        content_hash=uuid4().hex * 2,
        created_by=creator.id,
        status="READY",
        ocr_status="native",
        language="eng",
    )
    db.add(document)
    db.flush()
    return cabinet, folder, document


def _rule(
    db: Session,
    *,
    creator,
    principal,
    permission: str,
    scope_type: str,
    scope_id: int | None,
    effect: str = "ALLOW",
    inherits: bool = True,
):
    from app import models

    is_group = isinstance(principal, models.Group)
    value = models.AccessRule(
        principal_type="GROUP" if is_group else "USER",
        user_id=None if is_group else principal.id,
        group_id=principal.id if is_group else None,
        permission_id=_permission(db, permission).id,
        scope_type=scope_type,
        scope_id=scope_id,
        effect=effect,
        inherits=inherits,
        is_active=True,
        reason="direct-object authorization test",
        created_by=creator.id,
    )
    db.add(value)
    db.flush()
    return value


def _version(db: Session, document, *, user, test_paths) -> None:
    from app import models
    from app.utils import file_storage, hashing

    data = b"authorized document body"
    stored = file_storage.store_bytes(
        "restricted.txt",
        data,
        hashing.sha256_bytes(data),
    )
    db.add(
        models.DocVersion(
            document_id=document.id,
            version_no=1,
            file_key=stored.key,
            filename="restricted.txt",
            content_type="text/plain",
            size=len(data),
            checksum=hashing.sha256_bytes(data),
            ocr_text="authorized document body",
            created_by=user.id,
            created_at=datetime.now(UTC),
        )
    )
    db.flush()


def test_same_capability_users_get_different_direct_object_results(
    db_session: Session,
    user_factory: Callable[..., object],
    test_paths,
) -> None:
    from app.services import document_service
    from app.services.exceptions import NotFoundError

    alice = user_factory(username="direct-alice")
    bob = user_factory(username="direct-bob")
    _capabilities(db_session, alice, {"VIEW", "DOWNLOAD", "DELETE"})
    _capabilities(db_session, bob, {"VIEW", "DOWNLOAD", "DELETE"})
    cabinet, folder, document = _hierarchy(db_session, alice)
    _version(db_session, document, user=alice, test_paths=test_paths)

    # The users have the same capability bundle, but only Alice has the
    # explicit document ALLOW. Bob has a more specific DENY and cannot use a
    # guessed ID to distinguish the existing document from a missing one.
    for permission in ("VIEW", "DOWNLOAD", "DELETE"):
        _rule(
            db_session,
            creator=alice,
            principal=alice,
            permission=permission,
            scope_type="DOC",
            scope_id=document.id,
            inherits=False,
        )
        _rule(
            db_session,
            creator=alice,
            principal=bob,
            permission=permission,
            scope_type="DOC",
            scope_id=document.id,
            effect="DENY",
            inherits=False,
        )

    assert document_service.get_document_detail(db_session, alice, document.id).id == (
        document.id
    )
    with pytest.raises(NotFoundError, match="Document not found"):
        document_service.get_document_detail(db_session, bob, document.id)
    with pytest.raises(NotFoundError, match="Document not found"):
        document_service.get_document_detail(db_session, bob, 999_999)

    assert (
        document_service.get_download(db_session, alice, document.id).filename
        == "restricted.txt"
    )
    with pytest.raises(NotFoundError, match="Document not found"):
        document_service.get_download(db_session, bob, document.id)

    with pytest.raises(NotFoundError, match="Document not found"):
        document_service.delete_document(db_session, bob, document.id)
    document_service.delete_document(db_session, alice, document.id)
    tombstoned = db_session.get(type(document), document.id)
    assert tombstoned is not None
    assert tombstoned.lifecycle_state == "TOMBSTONED"


def test_folder_and_group_rules_authorize_import_target_without_existence_leak(
    db_session: Session,
    user_factory: Callable[..., object],
) -> None:
    from app import models
    from app.services import document_service
    from app.services.exceptions import NotFoundError

    creator = user_factory(username="import-creator")
    member = user_factory(username="import-member")
    _capabilities(db_session, member, {"CREATE"})
    _cabinet, folder, _ = _hierarchy(db_session, creator)
    other_folder = models.Folder(
        cabinet_id=folder.cabinet_id,
        name=f"other-folder-{uuid4().hex}",
    )
    db_session.add(other_folder)
    db_session.flush()
    group = models.Group(
        name=f"import-group-{uuid4().hex}",
        description="import ACL group",
        created_by=creator.id,
    )
    db_session.add(group)
    db_session.flush()
    db_session.add(
        models.GroupMembership(
            group_id=group.id,
            user_id=member.id,
            created_by=creator.id,
        )
    )
    db_session.flush()
    _rule(
        db_session,
        creator=creator,
        principal=group,
        permission="CREATE",
        scope_type="FOLDER",
        scope_id=folder.id,
        inherits=True,
    )

    assert document_service._folder_id(db_session, folder.id, user=member) == folder.id
    with pytest.raises(NotFoundError, match="Folder not found"):
        document_service._folder_id(db_session, other_folder.id, user=member)
    with pytest.raises(NotFoundError, match="Folder not found"):
        document_service._folder_id(db_session, 999_999, user=member)
