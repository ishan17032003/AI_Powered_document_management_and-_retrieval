from uuid import uuid4

import pytest

from app import models, schemas
from app.services import document_service
from app.services.exceptions import ConflictError


def _global_capability(db, user, code):
    permission = models.Permission(code=code)
    db.add(permission)
    db.flush()
    role = models.Role(name=f"docapi-{code}-{uuid4().hex}")
    db.add(role)
    db.flush()
    db.add(models.RolePermission(role_id=role.id, permission_id=permission.id))
    db.add(models.Assignment(user_id=user.id, role_id=role.id, scope_type="GLOBAL", effect="ALLOW"))
    db.flush()


def _document(db, user):
    cabinet = models.Cabinet(name=f"docapi-cabinet-{uuid4().hex}")
    db.add(cabinet)
    db.flush()
    folder = models.Folder(cabinet_id=cabinet.id, name="source")
    target = models.Folder(cabinet_id=cabinet.id, name="target")
    db.add_all([folder, target])
    db.flush()
    document = models.Document(folder_id=folder.id, title="metadata", content_hash="a" * 64, created_by=user.id, status="READY", ocr_status="native")
    db.add(document)
    db.flush()
    return document, target


def _document_rules(db, user, document, target):
    permissions = {row.code: row for row in db.query(models.Permission).all()}
    for code in ("VIEW", "EDIT_METADATA", "MOVE"):
        db.add(models.AccessRule(
            principal_type="USER",
            user_id=user.id,
            permission_id=permissions[code].id,
            scope_type="DOC",
            scope_id=document.id,
            effect="ALLOW",
            inherits=True,
            is_active=True,
            reason="DOCAPI test",
            created_by=user.id,
        ))
    db.add(models.AccessRule(
        principal_type="USER", user_id=user.id, permission_id=permissions["MOVE"].id,
        scope_type="FOLDER", scope_id=target.id, effect="ALLOW", inherits=True,
        is_active=True, reason="DOCAPI folder test", created_by=user.id,
    ))
    db.flush()


def test_metadata_update_and_move_require_current_timestamp(db_session, user_factory):
    user = user_factory(username="docapi-user")
    for code in ("VIEW", "EDIT_METADATA", "MOVE"):
        _global_capability(db_session, user, code)
    document, target = _document(db_session, user)
    _document_rules(db_session, user, document, target)
    db_session.commit()
    expected = document.updated_at
    result = document_service.update_metadata(
        db_session,
        user,
        document.id,
        schemas.DocumentMetadataUpdate(
            expected_updated_at=expected,
            metadata=[schemas.MetadataUpdate(key="department", value="records")],
        ),
    )
    assert result.metadata[0].value == "records"
    current = result.updated_at
    with pytest.raises(ConflictError):
        document_service.move_document(db_session, user, document.id, target.id, expected_updated_at=expected)
    moved = document_service.move_document(db_session, user, document.id, target.id, expected_updated_at=current)
    assert moved.folder_id == target.id
