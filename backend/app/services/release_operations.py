"""Release gates and operational-maturity evidence primitives."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class MigrationStep:
    revision: str
    checksum: str
    repair_id: str | None = None


def validate_migration_steps(steps: Iterable[MigrationStep]) -> tuple[MigrationStep, ...]:
    values = tuple(steps)
    if not values or any(not step.revision or len(step.checksum) != 64 for step in values):
        raise ValueError("tested migration checksums are required")
    if len({step.revision for step in values}) != len(values):
        raise ValueError("migration revisions must be unique")
    return values


@dataclass(frozen=True, slots=True)
class DerivedDataReconciliation:
    expected: int
    indexed: int
    missing: int
    stale: int
    rebuilt: int = 0

    @property
    def clean(self) -> bool:
        return self.missing == 0 and self.stale == 0


def validate_reconciliation(result: DerivedDataReconciliation) -> None:
    if min(result.expected, result.indexed, result.missing, result.stale, result.rebuilt) < 0:
        raise ValueError("reconciliation counters cannot be negative")
    if result.clean and result.indexed > result.expected:
        raise ValueError("reconciliation claims clean state with excess rows")


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    release_id: str
    application_digest: str
    migration_head: str
    enabled_lanes: tuple[str, ...] = ()
    traffic_percent: int = 0

    def validate(self) -> None:
        if not self.release_id or len(self.application_digest) != 64 or not self.migration_head:
            raise ValueError("release manifest provenance is incomplete")
        if not 0 <= self.traffic_percent <= 100:
            raise ValueError("traffic percentage is invalid")
        if any(lane not in {"text", "page", "image", "mixed"} for lane in self.enabled_lanes):
            raise ValueError("unsupported retrieval lane")


@dataclass(frozen=True, slots=True)
class SmokeCheckReport:
    checks: Mapping[str, bool]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


@dataclass(frozen=True, slots=True)
class ReadinessSignoff:
    score: float
    blockers: tuple[str, ...]
    signatures: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.score >= 7.5 and not self.blockers and bool(self.signatures)


def sign_readiness(*, score: float, blockers: Iterable[str], signatures: Iterable[str]) -> ReadinessSignoff:
    result = ReadinessSignoff(round(score, 2), tuple(sorted(set(blockers))), tuple(sorted(set(signatures))))
    if result.score < 0 or result.score > 10:
        raise ValueError("readiness score must be between zero and ten")
    return result


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    sequence: int
    evidence_type: str
    payload: Mapping[str, object]
    previous_hash: str
    entry_hash: str
    created_at: str


class ImmutableEvidenceLedger:
    """Append-only hash chain; export is deterministic and tamper-evident."""

    def __init__(self) -> None:
        self._entries: list[EvidenceEntry] = []

    def append(self, evidence_type: str, payload: Mapping[str, object]) -> EvidenceEntry:
        if not evidence_type or len(evidence_type) > 80:
            raise ValueError("evidence type is invalid")
        previous = self._entries[-1].entry_hash if self._entries else "0" * 64
        created = datetime.now(UTC).isoformat()
        canonical = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(f"{previous}|{evidence_type}|{canonical}|{created}".encode()).hexdigest()
        entry = EvidenceEntry(len(self._entries) + 1, evidence_type, dict(payload), previous, digest, created)
        self._entries.append(entry)
        return entry

    def verify(self) -> bool:
        previous = "0" * 64
        for entry in self._entries:
            canonical = json.dumps(dict(entry.payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            expected = hashlib.sha256(f"{previous}|{entry.evidence_type}|{canonical}|{entry.created_at}".encode()).hexdigest()
            if entry.previous_hash != previous or entry.entry_hash != expected:
                return False
            previous = entry.entry_hash
        return True

    def entries(self) -> tuple[EvidenceEntry, ...]:
        return tuple(self._entries)


@dataclass(frozen=True, slots=True)
class ServiceObjectives:
    availability: float
    p95_latency_ms: float
    freshness_minutes: float
    recovery_minutes: float
    support_hours: float
    retrieval_recall: float
    groundedness: float
    abstention: float
    acl_zero_leakage: bool


def validate_objectives(objectives: ServiceObjectives) -> None:
    rates = (objectives.availability, objectives.retrieval_recall, objectives.groundedness, objectives.abstention)
    if any(not 0 <= value <= 1 for value in rates) or any(value <= 0 for value in (objectives.p95_latency_ms, objectives.freshness_minutes, objectives.recovery_minutes, objectives.support_hours)):
        raise ValueError("service objectives are invalid")
    if not objectives.acl_zero_leakage:
        raise ValueError("ACL objective must require zero leakage")


@dataclass(frozen=True, slots=True)
class ErrorBudgetDecision:
    remaining_fraction: float
    freeze_non_remedial_changes: bool
    reason: str


def evaluate_error_budget(remaining_fraction: float, *, freeze_threshold: float = 0.0) -> ErrorBudgetDecision:
    if not 0 <= remaining_fraction <= 1 or not 0 <= freeze_threshold <= 1:
        raise ValueError("error budget fraction is invalid")
    freeze = remaining_fraction <= freeze_threshold
    return ErrorBudgetDecision(remaining_fraction, freeze, "budget_exhausted" if freeze else "budget_available")


__all__ = ["DerivedDataReconciliation", "ErrorBudgetDecision", "EvidenceEntry", "ImmutableEvidenceLedger", "MigrationStep", "ReadinessSignoff", "ReleaseManifest", "ServiceObjectives", "SmokeCheckReport", "evaluate_error_budget", "sign_readiness", "validate_migration_steps", "validate_objectives", "validate_reconciliation"]
