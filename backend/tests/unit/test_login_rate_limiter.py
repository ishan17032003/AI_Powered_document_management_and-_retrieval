"""Bounded account/source login throttling checks."""

from __future__ import annotations

import pytest

from app.services.login_rate_limiter import LoginRateLimiter
from app.utils.request_context import RequestContext


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_account_threshold_blocks_and_account_reset_clears_bucket() -> None:
    clock = _Clock()
    limiter = LoginRateLimiter(
        account_failure_limit=3,
        source_failure_limit=50,
        window_seconds=60,
        block_seconds=30,
        max_entries=20,
        clock=clock,
    )

    assert limiter.record_failure("alice", "198.51.100.10").allowed
    assert limiter.record_failure("alice", "198.51.100.10").allowed
    blocked = limiter.record_failure("alice", "198.51.100.10")
    assert not blocked.allowed
    assert blocked.dimension == "account"
    assert not limiter.check("alice", "198.51.100.10").allowed

    limiter.reset_account("alice")
    assert limiter.check("alice", "198.51.100.10").allowed
    assert limiter.record_failure("alice", "198.51.100.10").allowed


def test_source_threshold_is_independent_of_account_keys() -> None:
    limiter = LoginRateLimiter(
        account_failure_limit=50,
        source_failure_limit=3,
        window_seconds=60,
        block_seconds=30,
        max_entries=20,
    )

    assert limiter.record_failure("alice", "198.51.100.10").allowed
    assert limiter.record_failure("bob", "198.51.100.10").allowed
    blocked = limiter.record_failure("carol", "198.51.100.10")
    assert not blocked.allowed
    assert blocked.dimension == "source"
    assert not limiter.check("new-account", "198.51.100.10").allowed
    assert limiter.check("alice", "203.0.113.20").allowed


def test_block_expiry_and_window_expiry_restore_admission() -> None:
    clock = _Clock()
    limiter = LoginRateLimiter(
        account_failure_limit=2,
        source_failure_limit=50,
        window_seconds=20,
        block_seconds=5,
        max_entries=20,
        clock=clock,
    )
    limiter.record_failure("alice", "198.51.100.10")
    limiter.record_failure("alice", "198.51.100.10")
    assert not limiter.check("alice", "198.51.100.10").allowed

    clock.now = 5.1
    assert limiter.check("alice", "198.51.100.10").allowed

    clock.now = 20.1
    assert limiter.check("alice", "198.51.100.10").allowed
    # The expired entries are pruned, then fresh admission buckets are created
    # by this check; the bounded store therefore contains only those two.
    assert limiter.entry_count == 2


def test_memory_is_bounded_and_keys_are_not_retained_in_clear_text() -> None:
    limiter = LoginRateLimiter(
        account_failure_limit=100,
        source_failure_limit=100,
        max_entries=4,
    )
    for index in range(20):
        limiter.record_failure(f"user-{index}", f"198.51.100.{index + 1}")
    assert limiter.entry_count <= 4
    assert all("user-" not in key[1] for key in limiter._buckets)


def test_concurrent_admission_is_bounded_and_release_is_idempotent() -> None:
    limiter = LoginRateLimiter(
        account_failure_limit=2,
        source_failure_limit=10,
        max_entries=20,
    )
    first = limiter.acquire("alice", "198.51.100.10")
    second = limiter.acquire("alice", "198.51.100.10")
    third = limiter.acquire("alice", "198.51.100.10")
    assert first.allowed and second.allowed
    assert not third.allowed
    assert third.dimension == "account"
    limiter.release("alice", "198.51.100.10")
    limiter.release("alice", "198.51.100.10")
    # A duplicate release cannot underflow the reservation counter.
    limiter.release("alice", "198.51.100.10")
    assert limiter.acquire("alice", "198.51.100.10").allowed


def test_service_block_stays_generic_and_skips_second_lookup(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import auth_service

    limiter = LoginRateLimiter(
        account_failure_limit=1,
        source_failure_limit=10,
        max_entries=20,
    )
    monkeypatch.setattr(auth_service, "_login_limiter", limiter)
    monkeypatch.setattr(auth_service.audit_service, "record", lambda *_a, **_k: None)
    events: list[str] = []
    monkeypatch.setattr(
        auth_service,
        "emit_event",
        lambda event, **_kwargs: events.append(event),
    )
    lookups = 0

    def lookup(*_args: object, **_kwargs: object):
        nonlocal lookups
        lookups += 1
        return None

    monkeypatch.setattr(auth_service.user_repository, "get_by_username", lookup)
    monkeypatch.setattr(auth_service, "verify_password", lambda *_a, **_k: False)
    context = RequestContext(ip="198.51.100.10")

    for _ in range(2):
        with pytest.raises(auth_service.AuthenticationError) as rejected:
            auth_service.login(
                db_session,
                username="not-an-enumeration-oracle",
                password="wrong-password",
                context=context,
            )
        assert rejected.value.status_code == 401
        assert rejected.value.detail == "Incorrect username or password"

    assert lookups == 1
    assert events == ["auth.login.throttled"]
