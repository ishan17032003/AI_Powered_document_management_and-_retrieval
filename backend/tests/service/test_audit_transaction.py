from __future__ import annotations

import pytest
from sqlalchemy.orm import Session


def test_audit_record_is_transaction_neutral_and_rolls_back_with_action(
    db_session: Session, user_factory
) -> None:
    from app import models
    from app.services import audit_service

    actor = user_factory(username="audit-tx", email="audit-tx@example.test")
    audit_service.record(
        db_session,
        actor=actor,
        action="TEST_MUTATION",
        object_type="document",
        object_id=1,
    )
    db_session.flush()
    assert db_session.query(models.AuditLog).filter_by(action="TEST_MUTATION").one()

    db_session.rollback()
    assert db_session.query(models.AuditLog).filter_by(action="TEST_MUTATION").count() == 0


def test_audit_storage_failure_is_visible(
    db_session: Session, user_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories import audit_repository
    from app.services import audit_service

    actor = user_factory(username="audit-failure", email="audit-failure@example.test")

    def fail_add(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(audit_repository, "add", fail_add)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        audit_service.record(
            db_session,
            actor=actor,
            action="TEST_FAILURE",
            object_type="document",
            object_id=1,
        )
