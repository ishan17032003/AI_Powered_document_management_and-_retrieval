from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import check_security_acceptances as policy

TODAY = "2026-07-27"


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _acceptance(
    finding_key: str,
    scanner: str = "bandit",
    **overrides: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "SEC-ACCEPT-0001",
        "scanner": scanner,
        "finding_key": finding_key,
        "owner": "Backend owner",
        "approved_by": "Security owner",
        "expires": "2026-08-15",
        "reason": "The finding is not exploitable in this isolated code path.",
        "compensating_controls": (
            "The path is input constrained and covered by a regression test."
        ),
    }
    item.update(overrides)
    return item


def _register(path: Path, *acceptances: dict[str, Any]) -> Path:
    return _write_json(
        path,
        {"schema_version": 1, "acceptances": list(acceptances)},
    )


def _run(
    tmp_path: Path,
    scanner: str,
    report_payload: Any,
    *acceptances: dict[str, Any],
) -> int:
    report = _write_json(tmp_path / "report.json", report_payload)
    register = _register(tmp_path / "acceptances.json", *acceptances)
    return policy.main(
        [
            "--scanner",
            scanner,
            "--report",
            str(report),
            "--acceptances",
            str(register),
            "--today",
            TODAY,
        ]
    )


@pytest.mark.parametrize(
    ("scanner", "report"),
    [
        ("bandit", {"results": []}),
        ("gitleaks", []),
        ("pip-audit", {"dependencies": []}),
        ("trivy", {"Results": []}),
    ],
)
def test_empty_reports_pass(tmp_path: Path, scanner: str, report: Any) -> None:
    assert _run(tmp_path, scanner, report) == 0


def test_unaccepted_bandit_finding_fails_and_prints_stable_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {
        "results": [
            {
                "test_id": "B310",
                "filename": "./app/services/rag_service.py",
                "line_number": 90,
                "issue_severity": "MEDIUM",
                "issue_confidence": "HIGH",
                "issue_text": "raw scanner explanation",
            }
        ]
    }

    assert _run(tmp_path, "bandit", report) == 1
    output = capsys.readouterr().out
    assert "UNACCEPTED bandit|B310|MEDIUM|HIGH|app/services/rag_service.py|90" in output


def test_exact_current_acceptance_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "bandit|B310|MEDIUM|HIGH|app/services/rag_service.py|90"
    report = {
        "results": [
            {
                "test_id": "B310",
                "filename": "app/services/rag_service.py",
                "line_number": 90,
                "issue_severity": "MEDIUM",
                "issue_confidence": "HIGH",
            }
        ]
    }

    assert _run(tmp_path, "bandit", report, _acceptance(key)) == 0
    assert f"ACCEPTED {key} by SEC-ACCEPT-0001" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("expires", "message"),
    [
        ("2026-07-27", "expired"),
        ("2026-11-01", "exceeds the 90-day limit"),
        ("27-07-2026", "must use YYYY-MM-DD"),
    ],
)
def test_invalid_expiry_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    expires: str,
    message: str,
) -> None:
    acceptance = _acceptance(
        "bandit|B310|MEDIUM|HIGH|app/services/rag_service.py|90",
        expires=expires,
    )

    assert _run(tmp_path, "bandit", {"results": []}, acceptance) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", "TBD"),
        ("approved_by", ""),
        ("reason", "none"),
        ("compensating_controls", "N/A"),
    ],
)
def test_governance_fields_cannot_be_empty_or_placeholders(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    acceptance = _acceptance(
        "bandit|B310|MEDIUM|HIGH|app/services/rag_service.py|90",
        **{field: value},
    )

    assert _run(tmp_path, "bandit", {"results": []}, acceptance) == 2


def test_stale_acceptance_fails_after_finding_disappears(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    acceptance = _acceptance("bandit|B310|MEDIUM|HIGH|app/services/rag_service.py|90")

    assert _run(tmp_path, "bandit", {"results": []}, acceptance) == 2
    assert "stale acceptances" in capsys.readouterr().err


def test_duplicate_finding_acceptance_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "bandit|B310|MEDIUM|HIGH|app/services/rag_service.py|90"
    first = _acceptance(key)
    second = _acceptance(key, id="SEC-ACCEPT-0002")

    assert _run(tmp_path, "bandit", {"results": []}, first, second) == 2
    assert "duplicate acceptance for finding" in capsys.readouterr().err


def test_gitleaks_gate_never_prints_secret_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "should-never-appear-in-policy-output"
    report = [
        {
            "RuleID": "generic-api-key",
            "File": "backend/app/config.py",
            "StartLine": 18,
            "Secret": secret,
            "Match": f"token={secret}",
        }
    ]

    assert _run(tmp_path, "gitleaks", report) == 1
    output = capsys.readouterr().out
    assert secret not in output
    assert "gitleaks|generic-api-key|backend/app/config.py|18" in output


def test_pip_audit_key_includes_advisory_package_and_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {
        "dependencies": [
            {
                "name": "Example_Package",
                "version": "1.2.3",
                "vulns": [{"id": "CVE-2026-1234"}],
            }
        ]
    }

    assert _run(tmp_path, "pip-audit", report) == 1
    assert "pip-audit|CVE-2026-1234|example_package|1.2.3" in capsys.readouterr().out


def test_trivy_normalizes_vulnerability_misconfiguration_and_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {
        "Results": [
            {
                "Target": "docvault:scan (debian 13)",
                "Class": "os-pkgs",
                "Type": "debian",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-0001",
                        "Severity": "CRITICAL",
                        "PkgName": "libexample",
                        "InstalledVersion": "1.0",
                    }
                ],
                "Misconfigurations": [
                    {
                        "ID": "AVD-DS-0002",
                        "Severity": "HIGH",
                        "CauseMetadata": {"Resource": "Dockerfile.user"},
                    }
                ],
                "Secrets": [
                    {
                        "RuleID": "generic-secret",
                        "Severity": "HIGH",
                        "StartLine": 81,
                    }
                ],
            }
        ]
    }

    assert _run(tmp_path, "trivy", report) == 1
    output = capsys.readouterr().out
    assert (
        "trivy|vulnerability|CVE-2026-0001|CRITICAL|os-pkgs:debian|libexample|1.0|-"
    ) in output
    assert (
        "trivy|misconfiguration|AVD-DS-0002|HIGH|os-pkgs:debian|Dockerfile.user"
    ) in output
    assert "trivy|secret|generic-secret|HIGH|os-pkgs:debian|81" in output


def test_wrong_scanner_key_prefix_and_unknown_fields_fail(
    tmp_path: Path,
) -> None:
    wrong_prefix = _acceptance("gitleaks|rule|file|1", scanner="bandit")
    assert _run(tmp_path, "bandit", {"results": []}, wrong_prefix) == 2

    unknown_field = _acceptance(
        "bandit|B310|MEDIUM|HIGH|app/services/rag_service.py|90",
        ticket="SEC-1",
    )
    assert _run(tmp_path, "bandit", {"results": []}, unknown_field) == 2
