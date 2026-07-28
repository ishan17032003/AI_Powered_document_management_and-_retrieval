import pytest

from app.services import visual_telemetry
from app.services.visual_telemetry import VisualFeedback, build_visual_telemetry


def test_visual_telemetry_is_bounded_and_redacted(settings_env, monkeypatch):
    monkeypatch.setattr(visual_telemetry.settings, "secret_key", "secret")
    event = build_visual_telemetry(
        lane="image", outcome="success", query="private customer image",
        model_revision="model-v1", candidate_count=4, authorized_count=2,
        duration_ms=12.3456,
    )
    payload = event.as_dict()
    assert payload["query_digest"].startswith("hmac-sha256:")
    assert "private" not in str(payload)
    assert payload["duration_ms"] == 12.346


def test_feedback_accepts_only_bounded_categories():
    feedback = VisualFeedback("result-1", "irrelevant", "short note")
    assert feedback.audit_fields()["category"] == "irrelevant"
    with pytest.raises(ValueError):
        VisualFeedback("result-1", "prompt_injection")
