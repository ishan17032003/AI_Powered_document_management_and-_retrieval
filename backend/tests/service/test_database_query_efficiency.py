"""Query-shape regressions for DB-001 and DB-002."""

from __future__ import annotations

from sqlalchemy import event

from app import models
from app.services import admin_service, document_service


def _count_queries(db_session, callback):
    count = 0

    def before_cursor_execute(*_args):
        nonlocal count
        count += 1

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = callback()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return count, result


def test_admin_user_list_bulk_loads_roles(db_session, user_factory):
    users = [user_factory(username=f"db-user-{index}") for index in range(3)]
    role = models.Role(name="DB query role")
    db_session.add(role)
    db_session.flush()
    db_session.add_all(
        [models.Assignment(user_id=user.id, role_id=role.id, effect="ALLOW") for user in users]
    )
    db_session.flush()
    count, output = _count_queries(db_session, lambda: admin_service.list_users(db_session))
    assert len(output) >= 3
    assert all("DB query role" in item.roles for item in output if item.id in {user.id for user in users})
    assert count <= 2


def test_document_list_eager_loads_class_and_versions(db_session, user_factory):
    user = user_factory(username="db-document-user")
    cabinet = models.Cabinet(name="DB cabinet")
    db_session.add(cabinet)
    db_session.flush()
    folder = models.Folder(cabinet_id=cabinet.id, name="DB folder")
    db_session.add(folder)
    db_session.flush()
    doc_class = models.DocClass(name="DB class")
    db_session.add(doc_class)
    db_session.flush()
    document = models.Document(
        folder_id=folder.id,
        title="DB document",
        class_id=doc_class.id,
        content_hash="a" * 64,
        created_by=user.id,
        lifecycle_state="ACTIVE",
    )
    db_session.add(document)
    db_session.flush()
    db_session.add(models.DocVersion(
        document_id=document.id,
        version_no=1,
        file_key="objects/db/document",
        filename="document.txt",
        checksum="b" * 64,
        size=4,
        created_by=user.id,
        storage_state="AVAILABLE",
    ))
    db_session.flush()
    count, output = _count_queries(
        db_session,
        lambda: document_service.list_documents(db_session, allowed_ids={document.id}, limit=10),
    )
    assert output[0].doc_class == "DB class"
    assert output[0].size == 4
    assert count <= 3
