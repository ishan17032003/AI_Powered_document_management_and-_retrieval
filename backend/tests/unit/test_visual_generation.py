import pytest

from app.services import visual_generation
from app.services.multimodal_policy import QueryImagePolicy, QueryImagePolicyError


def test_visual_generation_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(visual_generation.settings, "visual_generation_enabled", False)
    result = visual_generation.generate_visual(prompt="describe this image")
    assert result == visual_generation.VisualGenerationResult(False, "none")


def test_visual_generation_enabled_requires_egress_policy(monkeypatch) -> None:
    monkeypatch.setattr(visual_generation.settings, "visual_generation_enabled", True)
    monkeypatch.setattr(visual_generation.settings, "visual_generation_provider", "ollama")
    monkeypatch.setattr(visual_generation, "provider_destination", lambda _provider: "http://localhost:11434")
    monkeypatch.setattr(visual_generation, "authorize_provider_destination", lambda *_args: None)
    with pytest.raises(QueryImagePolicyError):
        visual_generation.generate_visual(prompt="describe this image")
    result = visual_generation.generate_visual(
        prompt="describe this image",
        policy=QueryImagePolicy(allow_provider_egress=True),
    )
    assert result.enabled is True
    assert result.provider == "ollama"
    assert result.output is None
