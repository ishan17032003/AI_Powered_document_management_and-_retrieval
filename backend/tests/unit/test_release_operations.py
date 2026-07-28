import pytest

from app.services.release_operations import (
    DerivedDataReconciliation,
    ImmutableEvidenceLedger,
    MigrationStep,
    ReleaseManifest,
    ServiceObjectives,
    SmokeCheckReport,
    evaluate_error_budget,
    sign_readiness,
    validate_migration_steps,
    validate_objectives,
    validate_reconciliation,
)


def test_release_manifest_migrations_reconciliation_and_smoke_gate() -> None:
    validate_migration_steps([MigrationStep("20260727_0012", "a" * 64, "repair-1")])
    result = DerivedDataReconciliation(expected=10, indexed=10, missing=0, stale=0)
    validate_reconciliation(result)
    manifest = ReleaseManifest("release-1", "b" * 64, "20260727_0012", ("text",), 10)
    manifest.validate()
    assert SmokeCheckReport({"health": True, "acl": True}).passed


def test_readiness_requires_score_and_signatures() -> None:
    assert sign_readiness(score=8.0, blockers=(), signatures=("qa", "security")).ready
    assert not sign_readiness(score=8.0, blockers=("P1",), signatures=("qa",)).ready


def test_evidence_ledger_is_hash_chained_and_detects_mutation() -> None:
    ledger = ImmutableEvidenceLedger()
    ledger.append("test", {"passed": 3})
    ledger.append("release", {"id": "r1"})
    assert ledger.verify()
    entry = ledger.entries()[0]
    entry.payload["passed"] = 0  # type: ignore[index]
    assert not ledger.verify()


def test_objectives_and_error_budget_fail_closed() -> None:
    objectives = ServiceObjectives(0.99, 1000, 15, 60, 8, 0.8, 0.8, 0.8, True)
    validate_objectives(objectives)
    assert evaluate_error_budget(0.0).freeze_non_remedial_changes
    with pytest.raises(ValueError):
        validate_objectives(ServiceObjectives(0.99, 1000, 15, 60, 8, 0.8, 0.8, 0.8, False))
