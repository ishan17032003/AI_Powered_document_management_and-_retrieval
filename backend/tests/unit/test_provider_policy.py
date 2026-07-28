"""Regression coverage for fail-closed document-content egress."""

from __future__ import annotations

import json

import pytest


def test_unapproved_destination_is_denied_before_context_or_http_construction(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service
    from app.services.provider_policy import ProviderEgressDenied
    from app.utils.rag_types import Passage

    monkeypatch.setattr(rag_service.settings, "allow_external_llm", True)
    monkeypatch.setattr(
        rag_service.settings,
        "vllm_url",
        "https://blocked-provider.example.test/v1",
    )
    monkeypatch.setattr(
        rag_service.settings,
        "llm_allowed_hosts",
        ["approved-provider.example.test"],
    )

    context_construction: list[object] = []
    http_construction: list[object] = []

    def reject_context(*args, **kwargs):
        context_construction.append((args, kwargs))
        raise AssertionError("document context was constructed")

    def reject_http(*args, **kwargs):
        http_construction.append((args, kwargs))
        raise AssertionError("HTTP client was constructed")

    monkeypatch.setattr(rag_service, "_build_context", reject_context)
    monkeypatch.setattr(rag_service.httpx, "Client", reject_http)

    with pytest.raises(
        ProviderEgressDenied,
        match="destination_not_allowlisted",
    ) as raised:
        rag_service._answer_with_vllm(
            "private-question-canary",
            [
                Passage(
                    index=1,
                    document_id=1,
                    title="Private",
                    text="private-document-content-canary",
                )
            ],
        )

    assert context_construction == []
    assert http_construction == []
    rendered = str(raised.value)
    assert "blocked-provider" not in rendered
    assert "private-document-content-canary" not in rendered


def test_provider_diagnostics_are_passive_and_redacted(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    monkeypatch.setattr(rag_service.settings, "llm_provider", "vllm")
    monkeypatch.setattr(rag_service.settings, "allow_external_llm", True)
    monkeypatch.setattr(
        rag_service.settings,
        "vllm_url",
        "https://diagnostic-canary.example.test/v1",
    )
    monkeypatch.setattr(
        rag_service.settings,
        "llm_allowed_hosts",
        ["diagnostic-canary.example.test"],
    )

    def reject_probe(*_args, **_kwargs):
        raise AssertionError("status attempted provider or model initialization")

    monkeypatch.setattr(rag_service, "_answer_with_vllm", reject_probe)
    monkeypatch.setattr(rag_service, "_answer_with_ollama", reject_probe)
    monkeypatch.setattr(rag_service, "_get_client", reject_probe)
    monkeypatch.setattr(rag_service.search, "_get_embed_model", reject_probe)
    monkeypatch.setattr(rag_service.search, "_get_reranker", reject_probe)

    diagnostics = rag_service.status()
    rendered = json.dumps(diagnostics, sort_keys=True)

    assert diagnostics["provider_policy"] == {
        "provider": "vllm",
        "external_opt_in": True,
        "destination_policy": "allowed",
    }
    assert "diagnostic-canary" not in rendered
    assert "https://" not in rendered
