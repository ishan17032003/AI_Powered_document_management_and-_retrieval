"""Hard resource limits around provider generation."""

from __future__ import annotations

import sys
import time
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest


def _passage(text: str = "private provider context"):
    from app.utils.rag_types import Passage

    return Passage(index=1, document_id=7, title="Private", text=text)


def _allow_provider(
    rag_service,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    destination_field = f"{provider}_url"
    monkeypatch.setattr(rag_service.settings, "allow_external_llm", True)
    monkeypatch.setattr(rag_service.settings, destination_field, "http://provider")
    monkeypatch.setattr(rag_service.settings, "llm_allowed_hosts", ["provider"])


def test_provider_runner_enforces_total_deadline_and_rejects_saturation() -> None:
    from app.services.provider_runtime import ProviderRunner

    runner = ProviderRunner(max_concurrency=1)
    started = Event()
    release = Event()
    finished = Event()

    def blocked_call() -> str:
        started.set()
        release.wait(timeout=1)
        finished.set()
        return "late answer"

    before = time.monotonic()
    assert runner.run(blocked_call, total_timeout_seconds=0.05) is None
    elapsed = time.monotonic() - before

    assert started.is_set()
    assert elapsed < 0.25

    second_call_started = Event()

    def second_call() -> str:
        second_call_started.set()
        return "must not run"

    before = time.monotonic()
    assert (
        runner.run(
            second_call,
            total_timeout_seconds=0.5,
        )
        is None
    )
    assert time.monotonic() - before < 0.1
    assert not second_call_started.is_set()

    release.set()
    assert finished.wait(timeout=0.5)
    assert runner.run(lambda: "available", total_timeout_seconds=0.5) == "available"


def test_rag_composition_degrades_on_deadline_without_queueing_another_call(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service
    from app.services.provider_runtime import ProviderRunner

    started = Event()
    release = Event()
    finished = Event()
    calls: list[str] = []

    def blocked_vllm(*_args, **_kwargs) -> str:
        calls.append("vllm")
        started.set()
        release.wait(timeout=1)
        finished.set()
        return "late answer"

    monkeypatch.setattr(rag_service.settings, "llm_provider", "vllm")
    monkeypatch.setattr(
        rag_service.settings,
        "rag_provider_total_timeout_seconds",
        0.05,
    )
    monkeypatch.setattr(rag_service, "_provider_runner", ProviderRunner(1))
    monkeypatch.setattr(rag_service, "_answer_with_vllm", blocked_vllm)

    before = time.monotonic()
    first = rag_service._compose("question", [_passage()], scoped_id=7)
    assert time.monotonic() - before < 0.25
    assert started.is_set()
    assert first.mode == "extractive"

    second = rag_service._compose("question", [_passage()], scoped_id=7)
    assert second.mode == "extractive"
    assert calls == ["vllm"]

    release.set()
    assert finished.wait(timeout=0.5)


def test_context_is_bounded_by_encoded_bytes(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    monkeypatch.setattr(rag_service.settings, "rag_max_context_bytes", 73)
    context = rag_service._build_context(
        [
            _passage("निजी" * 100),
            _passage("must-not-appear"),
        ]
    )

    assert len(context.encode()) <= 73
    assert "must-not-appear" not in context
    context.encode().decode()


@pytest.mark.parametrize("provider", ["vllm", "ollama"])
def test_local_provider_http_and_output_limits_are_applied(
    provider: str,
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    captured: dict[str, Any] = {}
    _allow_provider(rag_service, monkeypatch, provider)
    monkeypatch.setattr(
        rag_service.settings,
        "rag_provider_connect_timeout_seconds",
        1.25,
    )
    monkeypatch.setattr(
        rag_service.settings,
        "rag_provider_read_timeout_seconds",
        4.5,
    )
    monkeypatch.setattr(rag_service.settings, "rag_provider_max_output_tokens", 73)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            if provider == "vllm":
                return {"choices": [{"message": {"content": "bounded"}}]}
            return {"message": {"content": "bounded"}}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def post(self, url: str, **kwargs: Any) -> Response:
            captured["url"] = url
            captured["post"] = kwargs
            return Response()

    monkeypatch.setattr(rag_service.httpx, "Client", Client)

    answer = (
        rag_service._answer_with_vllm("question", [_passage()])
        if provider == "vllm"
        else rag_service._answer_with_ollama("question", [_passage()])
    )

    timeout = captured["client"]["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (
        1.25,
        4.5,
        1.25,
        1.25,
    )
    assert "timeout" not in captured["post"]
    body = captured["post"]["json"]
    requested_tokens = (
        body["max_tokens"] if provider == "vllm" else body["options"]["num_predict"]
    )
    assert (answer, requested_tokens) == ("bounded", 73)


def test_anthropic_client_disables_retries_and_applies_same_limits(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    captured: dict[str, Any] = {}
    _allow_provider(rag_service, monkeypatch, "anthropic")
    monkeypatch.setattr(
        rag_service.settings,
        "rag_provider_connect_timeout_seconds",
        1.5,
    )
    monkeypatch.setattr(
        rag_service.settings,
        "rag_provider_read_timeout_seconds",
        6.0,
    )
    monkeypatch.setattr(rag_service, "_client", None)
    monkeypatch.setattr(rag_service, "_client_checked", False)

    class HttpClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["http"] = kwargs

    class AnthropicClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["anthropic"] = kwargs

    monkeypatch.setattr(rag_service.httpx, "Client", HttpClient)
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(Anthropic=AnthropicClient),
    )

    client = rag_service._get_client()

    assert isinstance(client, AnthropicClient)
    assert captured["anthropic"]["max_retries"] == 0
    timeout = captured["http"]["timeout"]
    assert (timeout.connect, timeout.read) == (1.5, 6.0)
    assert captured["http"]["follow_redirects"] is False
    assert captured["http"]["trust_env"] is False


def test_anthropic_output_token_limit_is_applied(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    captured: dict[str, Any] = {}
    _allow_provider(rag_service, monkeypatch, "anthropic")
    monkeypatch.setattr(rag_service.settings, "rag_provider_max_output_tokens", 81)

    class Messages:
        def create(self, **kwargs: Any):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="bounded")]
            )

    client = SimpleNamespace(messages=Messages())

    assert (
        rag_service._answer_with_claude(client, "question", [_passage()]) == "bounded"
    )
    assert captured["max_tokens"] == 81
