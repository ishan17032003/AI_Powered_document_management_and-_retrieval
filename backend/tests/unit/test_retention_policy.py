from datetime import UTC, datetime, timedelta

import pytest

from app import models
from app.services.exceptions import PermissionDeniedError
from app.services.retention_service import assert_deletable


def test_legal_hold_blocks_deletion() -> None:
    with pytest.raises(PermissionDeniedError):
        assert_deletable(models.Document(legal_hold=True))


def test_active_retention_blocks_and_expired_retention_allows() -> None:
    document = models.Document(
        retention_until=datetime.now(UTC) + timedelta(days=1)
    )
    with pytest.raises(PermissionDeniedError):
        assert_deletable(document)
    document.retention_until = datetime.now(UTC) - timedelta(seconds=1)
    assert_deletable(document)
