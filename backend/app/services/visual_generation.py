"""Independent, opt-in visual generation boundary (MM-030)."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from .multimodal_policy import (
    QueryImagePolicy,
    QueryImagePolicyError,
    assert_query_image_egress,
)
from .provider_policy import authorize_provider_destination, provider_destination


@dataclass(frozen=True, slots=True)
class VisualGenerationResult:
    enabled: bool
    provider: str
    output: str | None = None


def generate_visual(*, prompt: str, policy: QueryImagePolicy | None = None) -> VisualGenerationResult:
    """Return a gated generation result; never affect visual search availability."""
    if not settings.visual_generation_enabled or settings.visual_generation_provider == "none":
        return VisualGenerationResult(False, "none")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8_000:
        raise QueryImagePolicyError("visual generation prompt is invalid")
    provider = settings.visual_generation_provider
    destination = provider_destination(provider)
    assert_query_image_egress(policy or QueryImagePolicy(), destination=destination)
    authorize_provider_destination(provider, destination)
    # Provider adapters are intentionally a separate follow-up task; fail
    # closed instead of silently routing through text-RAG providers.
    return VisualGenerationResult(True, provider, None)


__all__ = ["VisualGenerationResult", "generate_visual"]
