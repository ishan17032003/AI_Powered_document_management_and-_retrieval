"""Fail-closed authorization for document-content LLM egress."""

from __future__ import annotations

import json
import os
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Settings, settings

NETWORKED_PROVIDERS = {"vllm", "ollama", "anthropic"}
SUPPORTED_PROVIDERS = NETWORKED_PROVIDERS | {"none"}
_HTTP_SCHEMES = {"http", "https"}


class ProviderEgressDenied(RuntimeError):
    """Raised before provider request or document-content construction."""

    def __init__(self, code: str, provider: str) -> None:
        safe_provider = provider if provider in SUPPORTED_PROVIDERS else "unsupported"
        self.code = code
        self.provider = safe_provider
        super().__init__(
            f"LLM provider egress denied ({code}; provider={safe_provider})"
        )


def _get_runtime_vllm_override() -> dict[str, str | list[str]]:
    """Fetch real-time vLLM URL and allowed-host overrides across worker processes."""
    for path in (Path("/data/vllm_runtime.json"), Path("/tmp/vllm_runtime.json")):
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    res: dict[str, str | list[str]] = {}
    env_url = os.environ.get("DOCVAULT_VLLM_URL")
    env_hosts = os.environ.get("DOCVAULT_LLM_ALLOWED_HOSTS")
    if env_url:
        res["vllm_url"] = env_url
    if env_hosts:
        res["llm_allowed_hosts"] = [h.strip() for h in env_hosts.split(",") if h.strip()]
    return res


def provider_destination(provider: str, config: Settings = settings) -> str:
    """Return the configured base URL without including it in diagnostics."""

    normalized = provider.strip().lower()
    if normalized == "vllm":
        override = _get_runtime_vllm_override()
        vllm_url = override.get("vllm_url")
        if isinstance(vllm_url, str) and vllm_url:
            return vllm_url
        return config.vllm_url
    if normalized == "ollama":
        return config.ollama_url
    if normalized == "anthropic":
        return config.anthropic_url
    raise ProviderEgressDenied("provider_not_networked", normalized)


def _is_internal_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return "." not in normalized
    return address.is_private or address.is_loopback or address.is_link_local


def _normalized_allowed_hosts(config: Settings) -> set[str]:
    hosts = {host.strip().rstrip(".").lower() for host in config.llm_allowed_hosts}
    override = _get_runtime_vllm_override()
    override_hosts = override.get("llm_allowed_hosts")
    if isinstance(override_hosts, list):
        for h in override_hosts:
            if isinstance(h, str) and h.strip():
                hosts.add(h.strip().rstrip(".").lower())
    return hosts


def authorize_provider_destination(
    provider: str,
    destination: str,
    config: Settings = settings,
) -> None:
    """Authorize one exact provider destination or raise a redacted error."""

    normalized_provider = provider.strip().lower()
    if normalized_provider not in NETWORKED_PROVIDERS:
        raise ProviderEgressDenied("provider_not_allowed", normalized_provider)
    if not config.allow_external_llm:
        raise ProviderEgressDenied("external_opt_in_required", normalized_provider)

    try:
        parsed = urlsplit(destination)
        hostname = (parsed.hostname or "").rstrip(".").lower()
    except ValueError as exc:
        raise ProviderEgressDenied("destination_invalid", normalized_provider) from exc

    if (
        parsed.scheme.lower() not in _HTTP_SCHEMES
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderEgressDenied("destination_invalid", normalized_provider)
    if (
        parsed.scheme.lower() == "http"
        and not _is_internal_host(hostname)
        and not config.allow_insecure_llm_http
    ):
        raise ProviderEgressDenied("secure_transport_required", normalized_provider)
    if hostname not in _normalized_allowed_hosts(config):
        raise ProviderEgressDenied("destination_not_allowlisted", normalized_provider)


def destination_is_allowed(provider: str, config: Settings = settings) -> bool:
    """Return policy state without exposing a URL, host, credential, or error."""

    try:
        authorize_provider_destination(
            provider,
            provider_destination(provider, config),
            config,
        )
    except ProviderEgressDenied:
        return False
    return True


def redacted_diagnostics(provider: str, config: Settings = settings) -> dict:
    """Return the minimum safe provider-policy state for authenticated status."""

    normalized = provider.strip().lower()
    safe_provider = normalized if normalized in SUPPORTED_PROVIDERS else "unsupported"
    if normalized == "none":
        destination_state = "not_applicable"
    else:
        destination_state = (
            "allowed" if destination_is_allowed(normalized, config) else "denied"
        )
    return {
        "provider": safe_provider,
        "external_opt_in": bool(config.allow_external_llm),
        "destination_policy": destination_state,
    }
