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


REASONING_LEVELS = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class ModelVersion:
    model_id: str
    display_version: str
    capabilities: tuple[str, ...] = ("text",)
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    default: bool = False
    # Reasoning levels the user may pick for this version; empty = fixed.
    reasoning_levels: tuple[str, ...] = ()
    default_reasoning: str = "low"


@dataclass(frozen=True)
class ProviderEntry:
    provider: str  # stable key used in SSE events ("openai" | "claude" | "gemini" | "vllm")
    display_name: str
    color: str
    versions: tuple[ModelVersion, ...] = field(default_factory=tuple)
    default_reasoning: str = "low"


_STATIC: tuple[ProviderEntry, ...] = (
    ProviderEntry(
        provider="openai",
        display_name="ChatGPT",
        color="#10a37f",
        versions=(
            # GPT-5.6 family (July 2026) — current OpenAI lineup. Sol input price
            # reflects the promotional short-context rate cut of 2026-08-22.
            ModelVersion("gpt-5.6-sol", "5.6 Sol", ("text", "code", "html", "report", "vision"), 4.00, 20.00, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gpt-5.6-terra", "5.6 Terra", ("text", "code", "html", "report", "vision"), 2.00, 12.00, default=True, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gpt-5.6-luna", "5.6 Luna", ("text", "code", "html"), 0.20, 1.20, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gpt-5.5", "GPT-5.5", ("text", "code", "html", "report", "vision"), 5.00, 30.00, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gpt-5.4", "GPT-5.4", ("text", "code", "html", "report"), 2.50, 15.00, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gpt-5.4-mini", "GPT-5.4 mini", ("text", "code", "html"), 0.75, 4.50, reasoning_levels=REASONING_LEVELS),
            # Legacy but still serviceable
            ModelVersion("gpt-4o", "GPT-4o", ("text", "code", "html", "report", "vision"), 2.50, 10.00),
            ModelVersion("gpt-4o-mini", "GPT-4o mini", ("text", "code", "html"), 0.15, 0.60),
        ),
    ),
    ProviderEntry(
        provider="claude",
        display_name="Claude",
        color="#d97757",
        versions=(
            ModelVersion("claude-haiku-4-5-20251001", "Haiku 4.5", ("text", "code", "html"), 0.80, 4.00, default=True, reasoning_levels=REASONING_LEVELS),
            ModelVersion("claude-sonnet-5", "Sonnet 5", ("text", "code", "html", "vision"), 2.00, 10.00, reasoning_levels=REASONING_LEVELS),
            ModelVersion("claude-opus-5", "Opus 5", ("text", "code", "html", "report", "vision"), 5.00, 25.00, reasoning_levels=REASONING_LEVELS),
            ModelVersion("claude-fable-5", "Fable 5", ("text", "code", "html", "report", "vision"), 10.00, 50.00, reasoning_levels=REASONING_LEVELS),
            ModelVersion("claude-sonnet-4-5-20250929", "Sonnet 4.5", ("text", "code", "html", "vision"), 2.00, 10.00, reasoning_levels=REASONING_LEVELS),
            ModelVersion("claude-opus-4-8", "Opus 4.8", ("text", "code", "html", "report", "vision"), 5.00, 25.00, reasoning_levels=REASONING_LEVELS),
        ),
    ),
    ProviderEntry(
        provider="gemini",
        display_name="Gemini",
        color="#4285f4",
        versions=(
            ModelVersion("gemini-2.5-flash", "2.5 Flash", ("text", "code", "html", "report", "vision"), 0.15, 0.60, default=True, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gemini-3.6-flash", "3.6 Flash", ("text", "code", "html"), 0.15, 0.60, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gemini-3.5-flash", "3.5 Flash", ("text", "code", "html"), 0.15, 0.60, reasoning_levels=REASONING_LEVELS),
            ModelVersion("gemini-3.7-flash", "3.7 Flash", ("text", "code", "html", "report", "vision"), 0.15, 0.60, reasoning_levels=REASONING_LEVELS),
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
            "default_reasoning": e.default_reasoning if e.provider != "vllm" else "none",
            "versions": [
                {
                    "model_id": v.model_id,
                    "display_version": v.display_version,
                    "capabilities": list(v.capabilities),
                    "pricing_per_mtok": {"in": v.price_in_per_mtok, "out": v.price_out_per_mtok},
                    "default": v.default,
                    "reasoning_levels": list(v.reasoning_levels),
                    "default_reasoning": v.default_reasoning if v.reasoning_levels else "none",
                }
                for v in e.versions
            ],
        }
        for e in entries
    ]


async def fetch_live_models(provider: str | None = None) -> dict[str, list[dict]]:
    """Query live provider APIs to discover available models for configured keys."""
    import httpx
    import os

    def _k(name: str, *env: str) -> str:
        val = getattr(settings, name, None) or ""
        for e in env:
            val = val or os.getenv(e) or ""
        return val.strip()

    results: dict[str, list[dict]] = {}
    
    # 1. OpenAI
    if provider in (None, "openai"):
        key = _k("openai_api_key", "DOCVAULT_OPENAI_API_KEY", "OPENAI_API_KEY")
        if key:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
                    if res.status_code == 200:
                        raw = res.json().get("data", [])
                        results["openai"] = [{"id": m.get("id"), "created": m.get("created")} for m in raw if "gpt" in m.get("id", "").lower() or "o1" in m.get("id", "").lower() or "o3" in m.get("id", "").lower()]
            except Exception:
                pass

    # 2. Claude (Anthropic)
    if provider in (None, "claude"):
        key = _k("anthropic_api_key", "DOCVAULT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
        if key:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.get("https://api.anthropic.com/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
                    if res.status_code == 200:
                        raw = res.json().get("data", [])
                        results["claude"] = [{"id": m.get("id"), "display_name": m.get("display_name")} for m in raw]
            except Exception:
                pass

    # 3. Gemini (Google)
    if provider in (None, "gemini"):
        key = _k("gemini_api_key", "DOCVAULT_GEMINI_API_KEY", "GEMINI_API_KEY")
        if key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=key)
                gem_models = []
                for m in genai.list_models():
                    if "generateContent" in m.supported_generation_methods:
                        gem_models.append({"id": m.name.replace("models/", ""), "name": m.display_name, "description": m.description})
                results["gemini"] = gem_models
            except Exception:
                pass

    # 4. vLLM (local)
    if provider in (None, "vllm"):
        url = _k("vllm_url", "DOCVAULT_VLLM_URL", "VLLM_URL").rstrip("/")
        if url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.get(f"{url}/models")
                    if res.status_code == 200:
                        raw = res.json().get("data", [])
                        results["vllm"] = [{"id": m.get("id")} for m in raw]
            except Exception:
                pass

    return results

