"""Small, fail-closed login throttling primitive.

This limiter is deliberately process-local.  It protects one web process from
credential spraying while a durable/shared limiter is still a separate
production requirement.  Keys are HMAC digests so account names and source
addresses are never retained in clear text or emitted in an event.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Callable, Final

DEFAULT_HASH_KEY: Final[bytes] = b"docvault-login-limiter-v1"


@dataclass
class _Bucket:
    failures: int = 0
    window_started: float = 0.0
    blocked_until: float = 0.0
    last_seen: float = 0.0


@dataclass(frozen=True)
class ThrottleDecision:
    """Safe decision metadata; no user-controlled key is included."""

    allowed: bool
    dimension: str = ""
    failure_count: int = 0


class LoginRateLimiter:
    """Bounded account and source failure windows.

    ``account_failure_limit`` and ``source_failure_limit`` are independent:
    either dimension can block a login.  Expired entries are removed on every
    operation and insertion evicts the least-recently-used entry, so attacker
    supplied usernames cannot grow memory without bound.
    """

    def __init__(
        self,
        *,
        account_failure_limit: int = 5,
        source_failure_limit: int = 20,
        window_seconds: float = 300.0,
        block_seconds: float = 60.0,
        max_entries: int = 10_000,
        enabled: bool = True,
        secret_key: str | bytes = DEFAULT_HASH_KEY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if account_failure_limit < 1 or source_failure_limit < 1:
            raise ValueError("failure limits must be positive")
        if window_seconds <= 0 or block_seconds <= 0:
            raise ValueError("throttle durations must be positive")
        if max_entries < 2:
            raise ValueError("max_entries must allow account and source buckets")
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8")
        self.account_failure_limit = account_failure_limit
        self.source_failure_limit = source_failure_limit
        self.window_seconds = float(window_seconds)
        self.block_seconds = float(block_seconds)
        self.max_entries = max_entries
        self.enabled = enabled
        self._secret_key = secret_key or DEFAULT_HASH_KEY
        self._clock = clock
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        # In-flight password checks are bounded per dimension.  This closes
        # the check-then-verify race where a burst of concurrent requests could
        # all pass admission before any failed attempt was recorded.
        self._inflight: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _bounded_text(value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        return value[:limit]

    def _key(self, dimension: str, value: object) -> tuple[str, str]:
        # Usernames are capped before hashing.  The source value has the same
        # bounded treatment even though RequestContext already validates IPs.
        limit = 80 if dimension == "account" else 64
        bounded = self._bounded_text(value, limit)
        digest = hmac.new(
            self._secret_key,
            dimension.encode("ascii") + b"\0" + bounded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return dimension, digest

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, bucket in self._buckets.items()
            if bucket.blocked_until <= now
            and now - bucket.window_started >= self.window_seconds
        ]
        for key in expired:
            self._buckets.pop(key, None)

    def _bucket(self, key: tuple[str, str], now: float) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_entries:
                oldest_key = min(
                    self._buckets,
                    key=lambda candidate: self._buckets[candidate].last_seen,
                )
                self._buckets.pop(oldest_key, None)
            bucket = _Bucket(window_started=now, last_seen=now)
            self._buckets[key] = bucket
        elif (
            bucket.blocked_until <= now
            and now - bucket.window_started >= self.window_seconds
        ):
            bucket.failures = 0
            bucket.window_started = now
            bucket.blocked_until = 0.0
            bucket.last_seen = now
        else:
            bucket.last_seen = now
        return bucket

    def check(self, account: object, source: object) -> ThrottleDecision:
        """Return a decision without changing failure counters."""

        if not self.enabled:
            return ThrottleDecision(allowed=True)
        now = self._clock()
        with self._lock:
            self._prune(now)
            for dimension, value, _limit in (
                ("account", account, self.account_failure_limit),
                ("source", source, self.source_failure_limit),
            ):
                bucket = self._bucket(self._key(dimension, value), now)
                if bucket.blocked_until > now:
                    return ThrottleDecision(
                        allowed=False,
                        dimension=dimension,
                        failure_count=bucket.failures,
                    )
        return ThrottleDecision(allowed=True)

    def acquire(self, account: object, source: object) -> ThrottleDecision:
        """Atomically admit one password verification.

        The reservation is released by :meth:`release`.  Reservations are
        intentionally bounded by the same account/source thresholds used for
        failures, limiting CPU spent on concurrent credential spraying.
        """

        if not self.enabled:
            return ThrottleDecision(allowed=True)
        now = self._clock()
        with self._lock:
            self._prune(now)
            for dimension, value, limit in (
                ("account", account, self.account_failure_limit),
                ("source", source, self.source_failure_limit),
            ):
                key = self._key(dimension, value)
                bucket = self._bucket(key, now)
                if bucket.blocked_until > now:
                    return ThrottleDecision(False, dimension, bucket.failures)
                if self._inflight.get(key, 0) >= limit:
                    return ThrottleDecision(False, dimension, bucket.failures)
            for dimension, value in (("account", account), ("source", source)):
                key = self._key(dimension, value)
                self._inflight[key] = self._inflight.get(key, 0) + 1
        return ThrottleDecision(allowed=True)

    def release(self, account: object, source: object) -> None:
        """Release a prior admission reservation (safe if called twice)."""

        if not self.enabled:
            return
        with self._lock:
            for dimension, value in (("account", account), ("source", source)):
                key = self._key(dimension, value)
                count = self._inflight.get(key, 0)
                if count <= 1:
                    self._inflight.pop(key, None)
                else:
                    self._inflight[key] = count - 1

    def record_failure(self, account: object, source: object) -> ThrottleDecision:
        """Record one failure and return whether it just caused a block."""

        if not self.enabled:
            return ThrottleDecision(allowed=True)
        now = self._clock()
        blocked_dimension = ""
        blocked_count = 0
        with self._lock:
            self._prune(now)
            for dimension, value, limit in (
                ("account", account, self.account_failure_limit),
                ("source", source, self.source_failure_limit),
            ):
                bucket = self._bucket(self._key(dimension, value), now)
                was_blocked = bucket.blocked_until > now
                bucket.failures += 1
                if not was_blocked and bucket.failures >= limit:
                    bucket.blocked_until = now + self.block_seconds
                    if not blocked_dimension:
                        blocked_dimension = dimension
                        blocked_count = bucket.failures
        return ThrottleDecision(
            allowed=not blocked_dimension,
            dimension=blocked_dimension,
            failure_count=blocked_count,
        )

    def reset_account(self, account: object) -> None:
        """Clear only the successfully authenticated account's failures."""

        if not self.enabled:
            return
        with self._lock:
            self._buckets.pop(self._key("account", account), None)

    def clear(self) -> None:
        """Clear all buckets; intended for process lifecycle and tests."""

        with self._lock:
            self._buckets.clear()
            self._inflight.clear()

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._buckets)


__all__ = ["LoginRateLimiter", "ThrottleDecision"]
