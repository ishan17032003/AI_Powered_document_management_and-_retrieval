from app.repositories import document_repository


def test_document_cursor_page_is_descending_and_authorization_bounded(db_session):
    rows = document_repository.list_after_id(
        db_session, allowed_ids={1, 2, 3}, after_id=None, limit=2
    )
    assert len(rows) <= 2
    assert [row.id for row in rows] == sorted((row.id for row in rows), reverse=True)
    if rows:
        next_page = document_repository.list_after_id(
            db_session,
            allowed_ids={1, 2, 3},
            after_id=rows[-1].id,
            limit=2,
        )
        assert all(row.id < rows[-1].id for row in next_page)
