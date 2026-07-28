"""Fail-closed, redacted validation for backend startup configuration."""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from .config import BASE_DIR, Settings, settings

MIN_PRODUCTION_TOKEN_MINUTES = 5
MAX_PRODUCTION_TOKEN_MINUTES = 60
MIN_SECRET_LENGTH = 32

_CHANGE_ME_MARKER = "change" + "-me"
_DEVELOPMENT_ONLY_MARKER = "dev" + "-only"
_UNSAFE_SECRET_VALUES = {
    _CHANGE_ME_MARKER,
    _CHANGE_ME_MARKER + "-in-production",
    "change" + "me",
    _DEVELOPMENT_ONLY_MARKER + "-" + _CHANGE_ME_MARKER,
    "password",
    "secret",
}
_FILESYSTEM_ROOT = Path("/")
_TEMPORARY_ROOTS = (
    _FILESYSTEM_ROOT / "tmp",
    _FILESYSTEM_ROOT / "var" / "tmp",
    _FILESYSTEM_ROOT / "dev" / "shm",
)
_HTTP_SCHEMES = {"http", "https"}
_LLM_PROVIDERS = {"vllm", "ollama", "anthropic", "none"}
_REMOVED_LLM_PROVIDERS = {"auto"}
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class ConfigurationIssue:
    """A safe startup finding that contains no configured value."""

    code: str
    field: str
    message: str


class StartupConfigurationError(RuntimeError):
    """Raised when production configuration violates the startup contract."""

    def __init__(self, issues: list[ConfigurationIssue]) -> None:
        self.issues = tuple(issues)
        lines = [
            f"DocVault startup configuration rejected ({len(self.issues)} issue(s))."
        ]
        lines.extend(
            f"- {issue.code} [{issue.field}]: {issue.message}" for issue in self.issues
        )
        super().__init__("\n".join(lines))


def _issue(code: str, field: str, message: str) -> ConfigurationIssue:
    return ConfigurationIssue(code=code, field=field, message=message)


def _split_url(
    value: str,
    *,
    field: str,
    code: str,
) -> tuple[SplitResult | None, list[ConfigurationIssue]]:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        parsed = None
        hostname = None
    if parsed is None or not parsed.scheme or not hostname:
        return None, [_issue(code, field, "must be an absolute URL with a host")]
    return parsed, []


def _is_internal_http_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        # A single-label name is expected to resolve only on the private service network.
        return "." not in normalized
    return address.is_private or address.is_loopback or address.is_link_local


def _http_url_issues(field: str, value: str) -> list[ConfigurationIssue]:
    parsed, issues = _split_url(value, field=field, code="CFG_URL_INVALID")
    if parsed is None:
        return issues

    if parsed.scheme.lower() not in _HTTP_SCHEMES:
        issues.append(
            _issue(
                "CFG_URL_SCHEME",
                field,
                "must use an HTTP or HTTPS scheme",
            )
        )
    if parsed.username is not None or parsed.password is not None:
        issues.append(
            _issue(
                "CFG_URL_CREDENTIALS",
                field,
                "must not embed credentials",
            )
        )
    if parsed.query or parsed.fragment:
        issues.append(
            _issue(
                "CFG_URL_COMPONENTS",
                field,
                "must not contain a query string or fragment",
            )
        )
    if (
        parsed.scheme.lower() == "http"
        and parsed.hostname is not None
        and not _is_internal_http_host(parsed.hostname)
    ):
        issues.append(
            _issue(
                "CFG_URL_TRANSPORT",
                field,
                "remote endpoints must use HTTPS",
            )
        )
    return issues


def _normalize_allowed_host(value: str) -> str | None:
    normalized = value.strip().rstrip(".").lower()
    if not normalized or "*" in normalized:
        return None
    try:
        ip_address(normalized)
        return normalized
    except ValueError:
        pass
    labels = normalized.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        return None
    return normalized


def _allowed_host_issues(hosts: list[str]) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    normalized = [_normalize_allowed_host(host) for host in hosts]
    if any(host is None for host in normalized):
        issues.append(
            _issue(
                "CFG_EGRESS_HOST_INVALID",
                "llm_allowed_hosts",
                "entries must be exact hostnames or IP addresses without wildcards or ports",
            )
        )
    valid = [host for host in normalized if host is not None]
    if len(valid) != len(set(valid)):
        issues.append(
            _issue(
                "CFG_EGRESS_HOST_DUPLICATE",
                "llm_allowed_hosts",
                "must not contain duplicate destination hosts",
            )
        )
    return issues


def _provider_egress_issues(
    config: Settings, provider: str
) -> list[ConfigurationIssue]:
    issues = _allowed_host_issues(config.llm_allowed_hosts)
    if provider not in {"vllm", "ollama", "anthropic"}:
        return issues

    field, destination = {
        "vllm": ("vllm_url", config.vllm_url),
        "ollama": ("ollama_url", config.ollama_url),
        "anthropic": ("anthropic_url", config.anthropic_url),
    }[provider]
    issues.extend(_http_url_issues(field, destination))

    if not config.allow_external_llm:
        issues.append(
            _issue(
                "CFG_EGRESS_OPT_IN",
                "allow_external_llm",
                "must be explicitly enabled for a networked LLM provider",
            )
        )

    parsed, _ = _split_url(
        destination,
        field=field,
        code="CFG_URL_INVALID",
    )
    allowed_hosts = {
        normalized
        for host in config.llm_allowed_hosts
        if (normalized := _normalize_allowed_host(host)) is not None
    }
    if (
        parsed is None
        or parsed.hostname is None
        or parsed.hostname.rstrip(".").lower() not in allowed_hosts
    ):
        issues.append(
            _issue(
                "CFG_EGRESS_HOST_DENIED",
                "llm_allowed_hosts",
                "must explicitly allow the selected provider destination host",
            )
        )
    return issues


def _provider_selection_issues(config: Settings) -> list[ConfigurationIssue]:
    raw = config.llm_provider
    provider = raw.strip().lower()
    if provider in _REMOVED_LLM_PROVIDERS:
        return [
            _issue(
                "CFG_PROVIDER_REMOVED",
                "llm_provider",
                "automatic routing is unsupported; select one provider or none",
            )
        ]
    if provider not in _LLM_PROVIDERS:
        return [
            _issue(
                "CFG_PROVIDER_UNKNOWN",
                "llm_provider",
                "must use a supported provider identifier",
            )
        ]
    if raw != provider:
        return [
            _issue(
                "CFG_PROVIDER_CANONICAL",
                "llm_provider",
                "must use the exact lowercase provider identifier",
            )
        ]
    return []


def _provider_resource_issues(config: Settings) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    numeric_limits = (
        ("rag_provider_connect_timeout_seconds", 0.1, 30.0),
        ("rag_provider_read_timeout_seconds", 0.1, 120.0),
        ("rag_provider_total_timeout_seconds", 0.5, 180.0),
        ("rag_provider_max_output_tokens", 1, 4096),
        ("rag_max_context_bytes", 1024, 1_048_576),
        ("rag_provider_max_concurrency", 1, 16),
    )
    for field, minimum, maximum in numeric_limits:
        value = getattr(config, field)
        non_finite = isinstance(value, float) and not math.isfinite(value)
        if non_finite or not minimum <= value <= maximum:
            issues.append(
                _issue(
                    "CFG_RAG_LIMIT",
                    field,
                    f"must be between {minimum} and {maximum}",
                )
            )

    if config.rag_provider_connect_timeout_seconds > (
        config.rag_provider_total_timeout_seconds
    ) or config.rag_provider_read_timeout_seconds > (
        config.rag_provider_total_timeout_seconds
    ):
        issues.append(
            _issue(
                "CFG_RAG_TIMEOUT_ORDER",
                (
                    "rag_provider_connect_timeout_seconds,"
                    "rag_provider_read_timeout_seconds,"
                    "rag_provider_total_timeout_seconds"
                ),
                "phase timeouts must not exceed the total provider deadline",
            )
        )
    return issues


def _database_issues(value: str) -> list[ConfigurationIssue]:
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed is None or not parsed.scheme:
        return [
            _issue(
                "CFG_DATABASE_URL",
                "database_url",
                "must be an absolute database URL",
            )
        ]
    scheme = parsed.scheme.lower()
    if scheme != "postgresql" and not scheme.startswith("postgresql+"):
        return [
            _issue(
                "CFG_DATABASE_PROFILE",
                "database_url",
                "shared production requires the approved PostgreSQL profile",
            )
        ]
    if not parsed.hostname:
        return [
            _issue(
                "CFG_DATABASE_URL",
                "database_url",
                "must be an absolute PostgreSQL URL with a host",
            )
        ]
    return []


def _secret_issues(value: str) -> list[ConfigurationIssue]:
    normalized = value.strip().lower()
    issues: list[ConfigurationIssue] = []
    if (
        normalized in _UNSAFE_SECRET_VALUES
        or _CHANGE_ME_MARKER in normalized
        or _DEVELOPMENT_ONLY_MARKER in normalized
    ):
        issues.append(
            _issue(
                "CFG_SECRET_DEFAULT",
                "secret_key",
                "must be injected and must not use a known placeholder",
            )
        )
    if len(value) < MIN_SECRET_LENGTH:
        issues.append(
            _issue(
                "CFG_SECRET_LENGTH",
                "secret_key",
                f"must contain at least {MIN_SECRET_LENGTH} characters",
            )
        )
    if value and len(set(value)) < 8:
        issues.append(
            _issue(
                "CFG_SECRET_VARIETY",
                "secret_key",
                "must not be a repeated or low-variety value",
            )
        )
    return issues


def _root_issues(field: str, value: Path) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    if not value.is_absolute():
        issues.append(
            _issue("CFG_ROOT_ABSOLUTE", field, "must be an absolute filesystem path")
        )

    resolved = value.resolve(strict=False)
    if resolved == Path("/"):
        issues.append(
            _issue("CFG_ROOT_BROAD", field, "must not be the filesystem root")
        )
    if resolved == BASE_DIR.resolve() or resolved.is_relative_to(BASE_DIR.resolve()):
        issues.append(
            _issue(
                "CFG_ROOT_SOURCE",
                field,
                "must be outside the application source directory",
            )
        )
    if any(
        resolved == temporary or resolved.is_relative_to(temporary)
        for temporary in _TEMPORARY_ROOTS
    ):
        issues.append(
            _issue(
                "CFG_ROOT_TEMPORARY",
                field,
                "must not use a temporary filesystem location",
            )
        )
    if value.is_symlink():
        issues.append(
            _issue(
                "CFG_ROOT_SYMLINK",
                field,
                "must not itself be a symbolic link",
            )
        )
    return issues


def _origins_issues(origins: list[str]) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    if not origins:
        return [
            _issue(
                "CFG_CORS_EMPTY",
                "cors_origins",
                "must name at least one exact production browser origin",
            )
        ]
    if len(origins) != len(set(origins)):
        issues.append(
            _issue(
                "CFG_CORS_DUPLICATE",
                "cors_origins",
                "must not contain duplicate origins",
            )
        )

    for origin in origins:
        if origin == "*":
            issues.append(
                _issue(
                    "CFG_CORS_WILDCARD",
                    "cors_origins",
                    "must not contain a wildcard origin",
                )
            )
            continue
        parsed, parsed_issues = _split_url(
            origin,
            field="cors_origins",
            code="CFG_CORS_INVALID",
        )
        issues.extend(parsed_issues)
        if parsed is None:
            continue
        if parsed.scheme.lower() != "https":
            issues.append(
                _issue(
                    "CFG_CORS_TRANSPORT",
                    "cors_origins",
                    "production browser origins must use HTTPS",
                )
            )
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path
        ):
            issues.append(
                _issue(
                    "CFG_CORS_EXACT",
                    "cors_origins",
                    "each entry must be an exact origin without credentials or path data",
                )
            )
    return issues


def _folder_import_issues(config: Settings) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    roots = config.folder_import_roots
    if config.folder_import_enabled and not roots:
        issues.append(
            _issue(
                "CFG_IMPORT_ROOTS_EMPTY",
                "folder_import_roots",
                "must name at least one approved root when folder import is enabled",
            )
        )

    resolved_roots: list[Path] = []
    for root in roots:
        issues.extend(_root_issues("folder_import_roots", root))
        resolved = root.resolve(strict=False)
        resolved_roots.append(resolved)
        storage = config.storage_dir.resolve(strict=False)
        okf = config.okf_bundle_dir.resolve(strict=False)
        if (
            resolved == storage
            or resolved.is_relative_to(storage)
            or storage.is_relative_to(resolved)
            or resolved == okf
            or resolved.is_relative_to(okf)
            or okf.is_relative_to(resolved)
        ):
            issues.append(
                _issue(
                    "CFG_IMPORT_ROOT_OVERLAP",
                    "folder_import_roots",
                    "must not overlap storage or OKF data roots",
                )
            )
    if len(resolved_roots) != len(set(resolved_roots)):
        issues.append(
            _issue(
                "CFG_IMPORT_ROOT_DUPLICATE",
                "folder_import_roots",
                "must not contain duplicate roots",
            )
        )

    limits = (
        ("folder_import_max_visited_entries", 1, 100_000),
        ("folder_import_max_files", 1, 10_000),
        ("folder_import_max_total_mb", 1, 102_400),
        ("folder_import_max_depth", 0, 100),
        ("folder_import_max_seconds", 1, 3_600),
    )
    for field, minimum, maximum in limits:
        if not minimum <= getattr(config, field) <= maximum:
            issues.append(
                _issue(
                    "CFG_IMPORT_LIMIT",
                    field,
                    f"must be between {minimum} and {maximum}",
                )
            )
    return issues


def production_configuration_issues(config: Settings) -> list[ConfigurationIssue]:
    """Return safe findings for the locked shared-production profile."""

    if not config.is_production:
        return []

    issues = _secret_issues(config.secret_key)
    issues.extend(_database_issues(config.database_url))
    issues.extend(_root_issues("storage_dir", config.storage_dir))
    issues.extend(_root_issues("okf_bundle_dir", config.okf_bundle_dir))

    storage = config.storage_dir.resolve(strict=False)
    okf = config.okf_bundle_dir.resolve(strict=False)
    if storage == okf or storage.is_relative_to(okf) or okf.is_relative_to(storage):
        issues.append(
            _issue(
                "CFG_ROOT_OVERLAP",
                "storage_dir,okf_bundle_dir",
                "production data roots must be distinct and non-overlapping",
            )
        )

    provider = config.llm_provider.strip().lower()
    provider_issues = _provider_selection_issues(config)
    issues.extend(provider_issues)
    if not provider_issues:
        issues.extend(_provider_egress_issues(config, provider))
    issues.extend(_provider_resource_issues(config))
    if config.use_qdrant:
        issues.extend(_http_url_issues("qdrant_url", config.qdrant_url))

    issues.extend(_origins_issues(config.cors_origins))
    issues.extend(_folder_import_issues(config))

    if config.debug:
        issues.append(
            _issue(
                "CFG_DEBUG_ENABLED",
                "debug",
                "must be disabled in production",
            )
        )
    if config.enable_demo_seed:
        issues.append(
            _issue(
                "CFG_DEMO_SEED_ENABLED",
                "enable_demo_seed",
                "must be disabled in production",
            )
        )
    if config.demo_fixed_credentials:
        issues.append(
            _issue(
                "CFG_FIXED_DEMO_CREDENTIALS",
                "demo_fixed_credentials",
                "must be disabled in production",
            )
        )
    if not (
        MIN_PRODUCTION_TOKEN_MINUTES
        <= config.access_token_minutes
        <= MAX_PRODUCTION_TOKEN_MINUTES
    ):
        issues.append(
            _issue(
                "CFG_TOKEN_LIFETIME",
                "access_token_minutes",
                (
                    "must be between "
                    f"{MIN_PRODUCTION_TOKEN_MINUTES} and "
                    f"{MAX_PRODUCTION_TOKEN_MINUTES} minutes"
                ),
            )
        )
    return issues


def startup_configuration_issues(config: Settings) -> list[ConfigurationIssue]:
    """Return common runtime issues plus the locked production policy."""

    issues = production_configuration_issues(config)
    if config.database_url.lower().startswith("sqlite") and config.ingestion_worker_count != 1:
        issues.append(
            _issue(
                "CFG_SQLITE_SINGLE_WORKER",
                "ingestion_worker_count",
                "SQLite ingestion requires exactly one worker",
            )
        )
    if not config.is_production:
        issues.extend(_provider_selection_issues(config))
        issues.extend(_provider_resource_issues(config))
    if not config.secret_key:
        issues.append(
            _issue(
                "CFG_SECRET_REQUIRED",
                "secret_key",
                "must be injected before the application runtime is initialized",
            )
        )
    return issues


def validate_startup(config: Settings) -> Settings:
    """Return validated settings or raise a value-redacted production error."""

    issues = startup_configuration_issues(config)
    if issues:
        raise StartupConfigurationError(issues)
    return config


def prepare_runtime_directories(config: Settings) -> None:
    """Create runtime directories only after production validation succeeds."""

    validate_startup(config)
    config.storage_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    config.okf_bundle_dir.mkdir(mode=0o750, parents=True, exist_ok=True)


def main() -> int:
    try:
        validate_startup(settings)
    except StartupConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    print(
        "DocVault startup configuration accepted "
        f"(environment={settings.environment.value})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
