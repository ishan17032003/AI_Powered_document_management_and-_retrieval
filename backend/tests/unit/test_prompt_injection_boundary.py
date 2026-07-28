"""Prompt-injection trust boundaries for every supported generation provider."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "prompt_injection_cases.json"
)
PROMPT_INJECTION_CASES: list[dict[str, str]] = json.loads(
    FIXTURE_PATH.read_text(encoding="utf-8")
)


def _passage(case: dict[str, str]):
    from app.utils.rag_types import Passage

    return Passage(
        index=1,
        document_id=7,
        title=case["title"],
        text=case["text"],
    )


def _allow_provider(
    rag_service,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    monkeypatch.setattr(rag_service.settings, "allow_external_llm", True)
    monkeypatch.setattr(rag_service.settings, f"{provider}_url", "http://provider")
    monkeypatch.setattr(rag_service.settings, "llm_allowed_hosts", ["provider"])


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for child in value.values() for key in _nested_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _nested_keys(child)}
    return set()


def _assert_untrusted_boundary(
    *,
    system: str,
    messages: list[dict[str, str]],
    case: dict[str, str],
) -> None:
    from app.services import rag_service

    assert system == rag_service._SYSTEM
    assert case["title"] not in system
    assert case["text"] not in system
    assert case["question"] not in system
    assert messages == [
        {
            "role": "user",
            "content": messages[0]["content"],
        }
    ]

    payload = json.loads(messages[0]["content"])
    assert set(payload) == {
        "schema",
        "trust",
        "document_excerpts",
        "user_request",
    }
    assert payload["schema"] == rag_service._UNTRUSTED_INPUT_SCHEMA
    assert payload["trust"] == "untrusted"
    assert payload["user_request"] == case["question"]
    assert case["title"] in payload["document_excerpts"]
    assert case["text"] in payload["document_excerpts"]


@pytest.mark.parametrize(
    "case",
    PROMPT_INJECTION_CASES,
    ids=[case["id"] for case in PROMPT_INJECTION_CASES],
)
@pytest.mark.parametrize("provider", ["vllm", "ollama", "anthropic"])
def test_injected_document_stays_out_of_system_policy_and_tool_surface(
    provider: str,
    case: dict[str, str],
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    captured: dict[str, Any] = {}
    _allow_provider(rag_service, monkeypatch, provider)

    if provider in {"vllm", "ollama"}:

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                if provider == "vllm":
                    return {"choices": [{"message": {"content": "safe"}}]}
                return {"message": {"content": "safe"}}

        class Client:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            def post(self, _url: str, **kwargs: Any) -> Response:
                captured.update(kwargs["json"])
                return Response()

        monkeypatch.setattr(rag_service.httpx, "Client", Client)
        answer = (
            rag_service._answer_with_vllm(case["question"], [_passage(case)])
            if provider == "vllm"
            else rag_service._answer_with_ollama(case["question"], [_passage(case)])
        )
        system_message, user_message = captured["messages"]
        system = system_message["content"]
        messages = [user_message]
    else:

        class Messages:
            def create(self, **kwargs: Any):
                captured.update(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="safe")]
                )

        client = SimpleNamespace(messages=Messages())
        answer = rag_service._answer_with_claude(
            client,
            case["question"],
            [_passage(case)],
        )
        system = captured["system"]
        messages = captured["messages"]

    assert answer == "safe"
    assert not {"tools", "tool_choice", "functions", "function_call"} & _nested_keys(
        captured
    )
    _assert_untrusted_boundary(system=system, messages=messages, case=case)


@pytest.mark.parametrize(
    "case",
    PROMPT_INJECTION_CASES,
    ids=[case["id"] for case in PROMPT_INJECTION_CASES],
)
@pytest.mark.parametrize("provider", ["vllm", "ollama", "anthropic"])
def test_injected_document_cannot_switch_the_configured_provider(
    provider: str,
    case: dict[str, str],
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    calls: list[str] = []
    client = object()

    def called(name: str):
        def answer(*_args: Any, **_kwargs: Any) -> str:
            calls.append(name)
            return "safe"

        return answer

    def must_not_run(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("injected content switched the configured provider")

    monkeypatch.setattr(rag_service.settings, "llm_provider", provider)
    monkeypatch.setattr(
        rag_service,
        "_answer_with_vllm",
        called("vllm") if provider == "vllm" else must_not_run,
    )
    monkeypatch.setattr(
        rag_service,
        "_answer_with_ollama",
        called("ollama") if provider == "ollama" else must_not_run,
    )
    monkeypatch.setattr(
        rag_service,
        "_get_client",
        (lambda: client) if provider == "anthropic" else must_not_run,
    )
    monkeypatch.setattr(
        rag_service,
        "_answer_with_claude",
        called("anthropic") if provider == "anthropic" else must_not_run,
    )

    result = rag_service._compose(
        case["question"],
        [_passage(case)],
        scoped_id=7,
    )

    assert calls == [provider]
    assert result.answer == "safe"


@pytest.mark.parametrize("provider", ["vllm", "ollama", "anthropic"])
def test_tool_only_provider_output_is_ignored(
    provider: str,
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rag_service

    case = PROMPT_INJECTION_CASES[1]
    _allow_provider(rag_service, monkeypatch, provider)

    if provider in {"vllm", "ollama"}:

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                message = {
                    "content": "",
                    "tool_calls": [{"name": "shell", "arguments": {"cmd": "sudo"}}],
                }
                if provider == "vllm":
                    return {"choices": [{"message": message}]}
                return {"message": message}

        class Client:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            def post(self, _url: str, **_kwargs: Any) -> Response:
                return Response()

        monkeypatch.setattr(rag_service.httpx, "Client", Client)
        answer = (
            rag_service._answer_with_vllm(case["question"], [_passage(case)])
            if provider == "vllm"
            else rag_service._answer_with_ollama(case["question"], [_passage(case)])
        )
    else:
        client = SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **_kwargs: SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="tool_use",
                            name="shell",
                            input={"cmd": "sudo"},
                        )
                    ]
                )
            )
        )
        answer = rag_service._answer_with_claude(
            client,
            case["question"],
            [_passage(case)],
        )

    assert answer is None
