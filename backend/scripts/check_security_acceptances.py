"""Fail security scans unless every finding has a narrow, current acceptance.

The scanner reports are intentionally kept outside the repository. This module
normalizes only non-secret identifiers and never prints a matched secret value.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

SCANNERS = ("bandit", "gitleaks", "pip-audit", "trivy")
MAX_ACCEPTANCE_DAYS = 90
ACCEPTANCE_ID = re.compile(r"^SEC-ACCEPT-\d{4}$")
PLACEHOLDERS = {"n/a", "na", "none", "tbd", "todo", "unknown"}


class PolicyError(ValueError):
    """Raised when a report or acceptance register is unsafe or malformed."""


@dataclass(frozen=True)
class Finding:
    scanner: str
    key: str
    summary: str


@dataclass(frozen=True)
class Acceptance:
    acceptance_id: str
    scanner: str
    finding_key: str
    owner: str
    approved_by: str
    expires: date


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read valid JSON from {path}: {exc}") from exc


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{context} must be a JSON object")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise PolicyError(f"{context} must be a JSON array")
    return value


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{context} must be a non-empty string")
    return value.strip()


def _line(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError(f"{context} must be a positive integer")
    return value


def _part(value: Any, context: str) -> str:
    text = _text(value, context).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.replace("|", "%7C").replace("\r", "").replace("\n", " ")


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    by_key: dict[str, Finding] = {}
    for finding in findings:
        previous = by_key.get(finding.key)
        if previous is not None and previous != finding:
            raise PolicyError(
                f"scanner produced a colliding finding key: {finding.key}"
            )
        by_key[finding.key] = finding
    return sorted(by_key.values(), key=lambda item: item.key)


def _bandit_findings(payload: Any) -> list[Finding]:
    report = _mapping(payload, "Bandit report")
    results = _list(report.get("results", []), "Bandit results")
    findings: list[Finding] = []
    for index, raw in enumerate(results):
        item = _mapping(raw, f"Bandit result {index}")
        rule = _part(item.get("test_id"), f"Bandit result {index} test_id")
        path = _part(item.get("filename"), f"Bandit result {index} filename")
        line = _line(item.get("line_number"), f"Bandit result {index} line_number")
        severity = _part(
            item.get("issue_severity"), f"Bandit result {index} issue_severity"
        )
        confidence = _part(
            item.get("issue_confidence"),
            f"Bandit result {index} issue_confidence",
        )
        findings.append(
            Finding(
                scanner="bandit",
                key=f"bandit|{rule}|{severity}|{confidence}|{path}|{line}",
                summary=f"{severity}/{confidence} {rule} at {path}:{line}",
            )
        )
    return _deduplicate(findings)


def _gitleaks_findings(payload: Any) -> list[Finding]:
    results = _list(payload, "Gitleaks report")
    findings: list[Finding] = []
    for index, raw in enumerate(results):
        item = _mapping(raw, f"Gitleaks result {index}")
        rule = _part(item.get("RuleID"), f"Gitleaks result {index} RuleID")
        path = _part(item.get("File"), f"Gitleaks result {index} File")
        line = _line(item.get("StartLine"), f"Gitleaks result {index} StartLine")
        findings.append(
            Finding(
                scanner="gitleaks",
                key=f"gitleaks|{rule}|{path}|{line}",
                summary=f"{rule} at {path}:{line}",
            )
        )
    return _deduplicate(findings)


def _pip_audit_findings(payload: Any) -> list[Finding]:
    report = _mapping(payload, "pip-audit report")
    dependencies = _list(report.get("dependencies", []), "pip-audit dependencies")
    findings: list[Finding] = []
    for dep_index, raw_dependency in enumerate(dependencies):
        dependency = _mapping(raw_dependency, f"pip-audit dependency {dep_index}")
        package = _part(
            dependency.get("name"), f"pip-audit dependency {dep_index} name"
        ).lower()
        version = _part(
            dependency.get("version"), f"pip-audit dependency {dep_index} version"
        )
        vulnerabilities = _list(
            dependency.get("vulns", []),
            f"pip-audit dependency {dep_index} vulnerabilities",
        )
        for vuln_index, raw_vulnerability in enumerate(vulnerabilities):
            vulnerability = _mapping(
                raw_vulnerability,
                f"pip-audit dependency {dep_index} vulnerability {vuln_index}",
            )
            vulnerability_id = _part(
                vulnerability.get("id"),
                (f"pip-audit dependency {dep_index} vulnerability {vuln_index} id"),
            )
            findings.append(
                Finding(
                    scanner="pip-audit",
                    key=(f"pip-audit|{vulnerability_id}|{package}|{version}"),
                    summary=(f"{vulnerability_id} affects {package} {version}"),
                )
            )
    return _deduplicate(findings)


def _optional_part(value: Any, default: str, context: str) -> str:
    if value is None or value == "":
        return default
    return _part(value, context)


def _trivy_findings(payload: Any) -> list[Finding]:
    report = _mapping(payload, "Trivy report")
    results = _list(report.get("Results", []), "Trivy results")
    findings: list[Finding] = []
    for result_index, raw_result in enumerate(results):
        result = _mapping(raw_result, f"Trivy result {result_index}")
        target = _optional_part(
            result.get("Target"), "-", f"Trivy result {result_index} Target"
        )
        result_class = _optional_part(
            result.get("Class"), "-", f"Trivy result {result_index} Class"
        )
        result_type = _optional_part(
            result.get("Type"), "-", f"Trivy result {result_index} Type"
        )
        scope = f"{result_class}:{result_type}"

        vulnerabilities = _list(
            result.get("Vulnerabilities", []),
            f"Trivy result {result_index} Vulnerabilities",
        )
        for finding_index, raw_finding in enumerate(vulnerabilities):
            finding = _mapping(
                raw_finding,
                f"Trivy result {result_index} vulnerability {finding_index}",
            )
            vulnerability_id = _part(
                finding.get("VulnerabilityID"),
                (
                    f"Trivy result {result_index} vulnerability "
                    f"{finding_index} VulnerabilityID"
                ),
            )
            package = _part(
                finding.get("PkgName"),
                (f"Trivy result {result_index} vulnerability {finding_index} PkgName"),
            )
            version = _part(
                finding.get("InstalledVersion"),
                (
                    f"Trivy result {result_index} vulnerability "
                    f"{finding_index} InstalledVersion"
                ),
            )
            package_path = _optional_part(
                finding.get("PkgPath"),
                "-",
                (f"Trivy result {result_index} vulnerability {finding_index} PkgPath"),
            )
            severity = _part(
                finding.get("Severity"),
                (f"Trivy result {result_index} vulnerability {finding_index} Severity"),
            )
            findings.append(
                Finding(
                    scanner="trivy",
                    key=(
                        f"trivy|vulnerability|{vulnerability_id}|{severity}|"
                        f"{scope}|{package}|{version}|{package_path}"
                    ),
                    summary=(
                        f"{severity} {vulnerability_id} affects {package} "
                        f"{version} in {target}"
                    ),
                )
            )

        misconfigurations = _list(
            result.get("Misconfigurations", []),
            f"Trivy result {result_index} Misconfigurations",
        )
        for finding_index, raw_finding in enumerate(misconfigurations):
            finding = _mapping(
                raw_finding,
                f"Trivy result {result_index} misconfiguration {finding_index}",
            )
            check_id = _part(
                finding.get("ID"),
                (f"Trivy result {result_index} misconfiguration {finding_index} ID"),
            )
            cause = finding.get("CauseMetadata")
            cause_mapping = (
                _mapping(
                    cause,
                    (
                        f"Trivy result {result_index} misconfiguration "
                        f"{finding_index} CauseMetadata"
                    ),
                )
                if cause is not None
                else {}
            )
            resource = _optional_part(
                cause_mapping.get("Resource"),
                "-",
                (
                    f"Trivy result {result_index} misconfiguration "
                    f"{finding_index} resource"
                ),
            )
            severity = _part(
                finding.get("Severity"),
                (
                    f"Trivy result {result_index} misconfiguration "
                    f"{finding_index} Severity"
                ),
            )
            findings.append(
                Finding(
                    scanner="trivy",
                    key=(
                        f"trivy|misconfiguration|{check_id}|{severity}|"
                        f"{scope}|{resource}"
                    ),
                    summary=f"{severity} {check_id} in {target} ({resource})",
                )
            )

        secrets = _list(
            result.get("Secrets", []), f"Trivy result {result_index} Secrets"
        )
        for finding_index, raw_finding in enumerate(secrets):
            finding = _mapping(
                raw_finding,
                f"Trivy result {result_index} secret {finding_index}",
            )
            rule = _part(
                finding.get("RuleID"),
                (f"Trivy result {result_index} secret {finding_index} RuleID"),
            )
            line = _line(
                finding.get("StartLine"),
                (f"Trivy result {result_index} secret {finding_index} StartLine"),
            )
            severity = _part(
                finding.get("Severity"),
                (f"Trivy result {result_index} secret {finding_index} Severity"),
            )
            findings.append(
                Finding(
                    scanner="trivy",
                    key=f"trivy|secret|{rule}|{severity}|{scope}|{line}",
                    summary=f"{severity} {rule} at {target}:{line}",
                )
            )
    return _deduplicate(findings)


PARSERS: dict[str, Callable[[Any], list[Finding]]] = {
    "bandit": _bandit_findings,
    "gitleaks": _gitleaks_findings,
    "pip-audit": _pip_audit_findings,
    "trivy": _trivy_findings,
}


def _meaningful_text(item: dict[str, Any], field: str, context: str) -> str:
    value = _text(item.get(field), f"{context} {field}")
    if value.casefold() in PLACEHOLDERS:
        raise PolicyError(f"{context} {field} cannot be a placeholder")
    return value


def _load_acceptances(path: Path, today: date) -> list[Acceptance]:
    register = _mapping(_load_json(path), "acceptance register")
    allowed_root_keys = {"schema_version", "acceptances"}
    unknown_root_keys = sorted(set(register) - allowed_root_keys)
    if unknown_root_keys:
        raise PolicyError(
            "acceptance register has unknown fields: " + ", ".join(unknown_root_keys)
        )
    if register.get("schema_version") != 1:
        raise PolicyError("acceptance register schema_version must be 1")

    raw_acceptances = _list(
        register.get("acceptances"), "acceptance register acceptances"
    )
    required_fields = {
        "id",
        "scanner",
        "finding_key",
        "owner",
        "approved_by",
        "expires",
        "reason",
        "compensating_controls",
    }
    acceptances: list[Acceptance] = []
    ids: set[str] = set()
    keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_acceptances):
        context = f"acceptance {index}"
        item = _mapping(raw, context)
        missing = sorted(required_fields - set(item))
        unknown = sorted(set(item) - required_fields)
        if missing:
            raise PolicyError(f"{context} is missing fields: {', '.join(missing)}")
        if unknown:
            raise PolicyError(f"{context} has unknown fields: {', '.join(unknown)}")

        acceptance_id = _text(item.get("id"), f"{context} id")
        if not ACCEPTANCE_ID.fullmatch(acceptance_id):
            raise PolicyError(f"{context} id must match SEC-ACCEPT-NNNN")
        if acceptance_id in ids:
            raise PolicyError(f"duplicate acceptance id: {acceptance_id}")
        ids.add(acceptance_id)

        scanner = _text(item.get("scanner"), f"{context} scanner")
        if scanner not in SCANNERS:
            raise PolicyError(
                f"{context} scanner must be one of: {', '.join(SCANNERS)}"
            )
        finding_key = _text(item.get("finding_key"), f"{context} finding_key")
        if not finding_key.startswith(f"{scanner}|"):
            raise PolicyError(f"{context} finding_key must start with {scanner}|")
        scanner_key = (scanner, finding_key)
        if scanner_key in keys:
            raise PolicyError(f"duplicate acceptance for finding: {finding_key}")
        keys.add(scanner_key)

        owner = _meaningful_text(item, "owner", context)
        approved_by = _meaningful_text(item, "approved_by", context)
        _meaningful_text(item, "reason", context)
        _meaningful_text(item, "compensating_controls", context)

        expires_text = _text(item.get("expires"), f"{context} expires")
        try:
            expires = date.fromisoformat(expires_text)
        except ValueError as exc:
            raise PolicyError(f"{context} expires must use YYYY-MM-DD") from exc
        if expires <= today:
            raise PolicyError(f"{acceptance_id} expired on {expires.isoformat()}")
        if expires > today + timedelta(days=MAX_ACCEPTANCE_DAYS):
            raise PolicyError(
                f"{acceptance_id} exceeds the {MAX_ACCEPTANCE_DAYS}-day limit"
            )

        acceptances.append(
            Acceptance(
                acceptance_id=acceptance_id,
                scanner=scanner,
                finding_key=finding_key,
                owner=owner,
                approved_by=approved_by,
                expires=expires,
            )
        )
    return acceptances


def evaluate(
    scanner: str,
    report_path: Path,
    acceptance_path: Path,
    today: date,
) -> tuple[list[Finding], list[tuple[Finding, Acceptance]], list[Finding]]:
    """Return all findings, accepted pairs, and unaccepted findings."""

    if scanner not in PARSERS:
        raise PolicyError(f"unsupported scanner: {scanner}")
    findings = PARSERS[scanner](_load_json(report_path))
    acceptances = _load_acceptances(acceptance_path, today)
    relevant = {
        acceptance.finding_key: acceptance
        for acceptance in acceptances
        if acceptance.scanner == scanner
    }
    finding_keys = {finding.key for finding in findings}
    stale = sorted(set(relevant) - finding_keys)
    if stale:
        raise PolicyError(
            f"{scanner} has stale acceptances with no matching finding: "
            + ", ".join(stale)
        )

    accepted: list[tuple[Finding, Acceptance]] = []
    unaccepted: list[Finding] = []
    for finding in findings:
        acceptance = relevant.get(finding.key)
        if acceptance is None:
            unaccepted.append(finding)
        else:
            accepted.append((finding, acceptance))
    return findings, accepted, unaccepted


def _parse_today(value: str | None) -> date:
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PolicyError("--today must use YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gate a redacted scanner JSON report against narrow, expiring "
            "security acceptances."
        )
    )
    parser.add_argument("--scanner", required=True, choices=SCANNERS)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--acceptances", required=True, type=Path)
    parser.add_argument(
        "--today",
        help="Policy date in YYYY-MM-DD; intended for deterministic tests.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        today = _parse_today(args.today)
        findings, accepted, unaccepted = evaluate(
            scanner=args.scanner,
            report_path=args.report,
            acceptance_path=args.acceptances,
            today=today,
        )
    except PolicyError as exc:
        print(f"Security acceptance policy error: {exc}", file=sys.stderr)
        return 2

    print(
        "Security acceptance gate: "
        f"scanner={args.scanner} findings={len(findings)} "
        f"accepted={len(accepted)} unaccepted={len(unaccepted)}"
    )
    for finding, acceptance in accepted:
        print(
            f"ACCEPTED {finding.key} by {acceptance.acceptance_id} "
            f"(owner={acceptance.owner}, approved_by={acceptance.approved_by}, "
            f"expires={acceptance.expires.isoformat()})"
        )
    for finding in unaccepted:
        print(f"UNACCEPTED {finding.key} — {finding.summary}")
    return 1 if unaccepted else 0


if __name__ == "__main__":
    raise SystemExit(main())
