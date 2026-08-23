"""Model registry for Ask AI agentic multi-model runs.

Single source of truth for which providers/versions the Ask AI comparison
grid may run, what each one can do, and how per-run cost is estimated.
Served to the SPA via ``GET /api/v1/search/ask/models`` so the version
picker is never hardcoded client-side.

Pricing is expressed in USD per 1M tokens (input, output) and is an
operator-maintained estimate used for the card footer metrics — not a
billing system of record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...config import settings


@dataclass(frozen=True)
class ModelVersion:
    model_id: str
    display_version: str
    capabilities: tuple[str, ...] = ("text",)
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    default: bool = False


@dataclass(frozen=True)
class ProviderEntry:
    provider: str  # stable key used in SSE events ("openai" | "claude" | "gemini" | "vllm")
    display_name: str
    color: str
    versions: tuple[ModelVersion, ...] = field(default_factory=tuple)


_STATIC: tuple[ProviderEntry, ...] = (
    ProviderEntry(
        provider="openai",
        display_name="ChatGPT",
        color="#10a37f",
        versions=(
            ModelVersion("gpt-4o", "GPT-4o", ("text", "code", "html", "report", "vision"), 2.50, 10.00, default=True),
            ModelVersion("gpt-4o-mini", "GPT-4o mini", ("text", "code", "html"), 0.15, 0.60),
        ),
    ),
    ProviderEntry(
        provider="claude",
        display_name="Claude",
        color="#d97757",
        versions=(
            ModelVersion("claude-sonnet-4-5", "Sonnet 4.5", ("text", "code", "html", "report", "vision"), 3.00, 15.00, default=True),
            ModelVersion("claude-opus-4-8", "Opus 4.8", ("text", "code", "html", "report", "vision"), 15.00, 75.00),
            ModelVersion("claude-haiku-4-5-20251001", "Haiku 4.5", ("text", "code", "html"), 1.00, 5.00),
        ),
    ),
    ProviderEntry(
        provider="gemini",
        display_name="Gemini",
        color="#4285f4",
        versions=(
            ModelVersion("gemini-2.5-pro", "2.5 Pro", ("text", "code", "html", "report", "vision"), 1.25, 10.00, default=True),
            ModelVersion("gemini-2.5-flash", "2.5 Flash", ("text", "code", "html"), 0.30, 2.50),
        ),
    ),
)


def _vllm_entry() -> ProviderEntry:
    model = (settings.vllm_model or "local-model").strip()
    return ProviderEntry(
        provider="vllm",
        display_name="vLLM (local)",
        color="#7c3aed",
        versions=(ModelVersion(model, model, ("text", "code", "html"), 0.0, 0.0, default=True),),
    )


def available_providers(configured: list[str]) -> list[ProviderEntry]:
    """Registry entries for providers that are actually configured."""
    entries = [e for e in _STATIC if e.provider in configured]
    if "vllm" in configured:
        entries.append(_vllm_entry())
    return entries


def resolve(provider: str, model_id: str | None, configured: list[str]) -> ModelVersion | None:
    """Resolve a requested (provider, model_id) to a registry version.

    Falls back to the provider default when model_id is None or unknown —
    unknown ids are never forwarded verbatim to a provider API.
    """
    for entry in available_providers(configured):
        if entry.provider != provider:
            continue
        for v in entry.versions:
            if model_id is not None and v.model_id == model_id:
                return v
        for v in entry.versions:
            if v.default:
                return v
        return entry.versions[0] if entry.versions else None
    return None


def estimate_cost_usd(version: ModelVersion, tokens_in: int, tokens_out: int) -> float:
    return round(
        (tokens_in * version.price_in_per_mtok + tokens_out * version.price_out_per_mtok) / 1_000_000,
        6,
    )


def to_public(entries: list[ProviderEntry]) -> list[dict]:
    return [
        {
            "provider": e.provider,
            "display_name": e.display_name,
            "color": e.color,
            "versions": [
                {
                    "model_id": v.model_id,
                    "display_version": v.display_version,
                    "capabilities": list(v.capabilities),
                    "pricing_per_mtok": {"in": v.price_in_per_mtok, "out": v.price_out_per_mtok},
                    "default": v.default,
                }
                for v in e.versions
            ],
        }
        for e in entries
    ]
