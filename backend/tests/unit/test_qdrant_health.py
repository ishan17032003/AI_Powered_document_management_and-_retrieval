"""Recovery and bounded-circuit checks for the optional Qdrant dependency."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from app import config
from app.repositories import search_repository


class _RecoveringClient:
    def get_collections(self) -> object:
        return SimpleNamespace(collections=[])


def test_qdrant_probe_recovers_after_transient_failure(monkeypatch) -> None:
    attempts = 0

    class QdrantClient:
        def __new__(cls, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("transient outage")
            return _RecoveringClient()

    package = ModuleType("qdrant_client")
    package.QdrantClient = QdrantClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qdrant_client", package)
    monkeypatch.setattr(config.settings, "use_qdrant", True)
    search_repository.reset_qdrant_state()

    assert search_repository.get_qdrant() is None
    assert attempts == 1
    assert search_repository.qdrant_health_status().state == "cooldown"
    # A request during cooldown must not stampede the provider.
    assert search_repository.get_qdrant() is None
    assert attempts == 1

    # Simulate expiry of the bounded retry delay without waiting in a test.
    search_repository._qdrant_next_probe_at = 0.0
    client = search_repository.get_qdrant()
    assert isinstance(client, _RecoveringClient)
    assert attempts == 2
    assert search_repository.qdrant_is_ready() is True
    assert search_repository.qdrant_health_status().state == "ready"


def test_qdrant_health_is_safe_and_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "use_qdrant", False)
    search_repository.reset_qdrant_state()
    status = search_repository.qdrant_health_status()
    assert status.state == "disabled"
    assert status.failures == 0
    assert status.retry_after_seconds == 0
