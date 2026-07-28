"""Exactly-one-provider generation routing."""

from __future__ import annotations

import pytest


def _passage():
    from app.utils.rag_types import Passage

    return Passage(
        index=1,
        document_id=7,
        title="Private",
        text="provider-routing-content-canary",
    )


def _must_not_run(*_args, **_kwargs):
    raise AssertionError("an unconfigured provider path was attempted")


def test_explicit_vllm_attempts_only_vllm(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    calls: list[str] = []

    def answer_with_vllm(*_args, **_kwargs) -> str:
        calls.append("vllm")
        return "vLLM answer"

    monkeypatch.setattr(rag_service.settings, "llm_provider", "vllm")
    monkeypatch.setattr(rag_service, "_answer_with_vllm", answer_with_vllm)
    monkeypatch.setattr(rag_service, "_answer_with_ollama", _must_not_run)
    monkeypatch.setattr(rag_service, "_get_client", _must_not_run)

    result = rag_service._compose("question", [_passage()], scoped_id=7)

    assert calls == ["vllm"]
    assert (result.mode, result.answer, result.model) == (
        "vllm",
        "vLLM answer",
        rag_service.settings.vllm_model,
    )


def test_explicit_ollama_attempts_only_ollama(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    calls: list[str] = []

    def answer_with_ollama(*_args, **_kwargs) -> str:
        calls.append("ollama")
        return "Ollama answer"

    monkeypatch.setattr(rag_service.settings, "llm_provider", "ollama")
    monkeypatch.setattr(rag_service, "_answer_with_vllm", _must_not_run)
    monkeypatch.setattr(rag_service, "_answer_with_ollama", answer_with_ollama)
    monkeypatch.setattr(rag_service, "_get_client", _must_not_run)

    result = rag_service._compose("question", [_passage()], scoped_id=7)

    assert calls == ["ollama"]
    assert (result.mode, result.answer, result.model) == (
        "ollama",
        "Ollama answer",
        rag_service.settings.ollama_model,
    )


def test_explicit_anthropic_attempts_only_anthropic(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    client = object()
    calls: list[str] = []

    def answer_with_anthropic(actual, *_args, **_kwargs) -> str:
        if actual is not client:
            _must_not_run()
        calls.append("anthropic")
        return "Anthropic answer"

    monkeypatch.setattr(rag_service.settings, "llm_provider", "anthropic")
    monkeypatch.setattr(rag_service, "_answer_with_vllm", _must_not_run)
    monkeypatch.setattr(rag_service, "_answer_with_ollama", _must_not_run)
    monkeypatch.setattr(rag_service, "_get_client", lambda: client)
    monkeypatch.setattr(rag_service, "_answer_with_claude", answer_with_anthropic)

    result = rag_service._compose("question", [_passage()], scoped_id=7)

    assert calls == ["anthropic"]
    assert (result.mode, result.answer, result.model) == (
        "claude",
        "Anthropic answer",
        rag_service.settings.rag_model,
    )


@pytest.mark.parametrize("provider", ["vllm", "ollama", "anthropic"])
def test_configured_provider_failure_never_falls_through_to_another_provider(
    provider: str,
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    monkeypatch.setattr(rag_service.settings, "llm_provider", provider)
    monkeypatch.setattr(
        rag_service,
        "_answer_with_vllm",
        (lambda *_args, **_kwargs: None) if provider == "vllm" else _must_not_run,
    )
    monkeypatch.setattr(
        rag_service,
        "_answer_with_ollama",
        (lambda *_args, **_kwargs: None) if provider == "ollama" else _must_not_run,
    )
    monkeypatch.setattr(
        rag_service,
        "_get_client",
        (lambda: None) if provider == "anthropic" else _must_not_run,
    )

    result = rag_service._compose("question", [_passage()], scoped_id=7)

    assert result.mode == "extractive"
    assert "provider-routing-content-canary" in result.answer
