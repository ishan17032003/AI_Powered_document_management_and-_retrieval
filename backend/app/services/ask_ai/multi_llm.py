"""Parallel multi-model streaming for Ask AI.

Each configured provider runs concurrently against the identical prompt.
Per run the stream emits, as SSE ``data:`` lines:

  {"type": "run_started",   "provider", "model_id", "display_version"}
  {"type": "chunk",         "provider", "chunk"}                (many)
  {"type": "run_completed", "provider", "model_id",
   "metrics": {"tokens_in", "tokens_out", "cost_usd", "latency_ms"}}
  {"type": "done"}                                              (once, at end)

Providers report true token usage where their streaming API exposes it;
otherwise a chars/4 estimate is used. Cost comes from the model registry.
"""

import asyncio
import json
import logging
import os
import time
from typing import AsyncGenerator

import httpx

from ...config import settings
from . import model_registry

_log = logging.getLogger(__name__)


def _key(name: str, *env: str) -> str:
    value = getattr(settings, name, None) or ""
    for e in env:
        value = value or os.getenv(e) or ""
    return value.strip()


def get_configured_providers() -> list[str]:
    providers = []
    if _key("openai_api_key", "DOCVAULT_OPENAI_API_KEY", "OPENAI_API_KEY"):
        providers.append("openai")
    if _key("anthropic_api_key", "DOCVAULT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        providers.append("claude")
    if _key("gemini_api_key", "DOCVAULT_GEMINI_API_KEY", "GEMINI_API_KEY"):
        providers.append("gemini")
    if _key("vllm_url", "DOCVAULT_VLLM_URL", "VLLM_URL"):
        providers.append("vllm")
    return providers


# ── Provider streams: yield {"text": ...} chunks and one optional {"usage": ...} ──


async def _stream_openai(messages, api_key: str, model_id: str):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    stream = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        stream=True,
        temperature=0.2,
        stream_options={"include_usage": True},
    )
    async for chunk in stream:
        if chunk.choices:
            content = chunk.choices[0].delta.content
            if content:
                yield {"text": content}
        usage = getattr(chunk, "usage", None)
        if usage:
            yield {"usage": {"in": usage.prompt_tokens or 0, "out": usage.completion_tokens or 0}}


async def _stream_anthropic(messages, api_key: str, model_id: str):
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    system = ""
    anthropic_messages = []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        else:
            anthropic_messages.append({"role": m["role"], "content": m["content"]})
    async with client.messages.stream(
        max_tokens=1024,
        system=system,
        messages=anthropic_messages,
        model=model_id,
        temperature=0.2,
    ) as stream:
        async for text in stream.text_stream:
            yield {"text": text}
        final = await stream.get_final_message()
        if final and final.usage:
            yield {"usage": {"in": final.usage.input_tokens or 0, "out": final.usage.output_tokens or 0}}


async def _stream_gemini(messages, api_key: str, model_id: str):
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    system = ""
    gemini_messages = []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        elif m["role"] == "user":
            gemini_messages.append({"role": "user", "parts": [m["content"]]})
        elif m["role"] == "assistant":
            gemini_messages.append({"role": "model", "parts": [m["content"]]})
    model = genai.GenerativeModel(model_id, system_instruction=system)
    response = await model.generate_content_async(gemini_messages, stream=True)
    usage = None
    async for chunk in response:
        if chunk.text:
            yield {"text": chunk.text}
        meta = getattr(chunk, "usage_metadata", None)
        if meta and getattr(meta, "candidates_token_count", 0):
            usage = {"in": meta.prompt_token_count or 0, "out": meta.candidates_token_count or 0}
    if usage:
        yield {"usage": usage}


async def _stream_vllm(messages, vllm_url: str, model_id: str):
    url = f"{vllm_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
        "max_tokens": 1024,
        "stream_options": {"include_usage": True},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, json=payload) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices")
                if choices:
                    content = choices[0].get("delta", {}).get("content", "")
                    if content:
                        yield {"text": content}
                usage = data.get("usage")
                if usage and usage.get("completion_tokens"):
                    yield {"usage": {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0)}}


_STREAMS = {
    "openai": lambda messages, model_id: _stream_openai(messages, _key("openai_api_key", "DOCVAULT_OPENAI_API_KEY", "OPENAI_API_KEY"), model_id),
    "claude": lambda messages, model_id: _stream_anthropic(messages, _key("anthropic_api_key", "DOCVAULT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"), model_id),
    "gemini": lambda messages, model_id: _stream_gemini(messages, _key("gemini_api_key", "DOCVAULT_GEMINI_API_KEY", "GEMINI_API_KEY"), model_id),
    "vllm": lambda messages, model_id: _stream_vllm(messages, _key("vllm_url", "DOCVAULT_VLLM_URL", "VLLM_URL"), model_id),
}


async def _run_model(
    provider: str,
    version: model_registry.ModelVersion,
    messages: list[dict],
) -> AsyncGenerator[dict, None]:
    """Run one provider/version: emit run_started, chunks, run_completed."""
    started = time.monotonic()
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    out_chars = 0
    usage: dict | None = None
    yield {
        "type": "run_started",
        "provider": provider,
        "model_id": version.model_id,
        "display_version": version.display_version,
    }
    try:
        async for event in _STREAMS[provider](messages, version.model_id):
            if "text" in event:
                out_chars += len(event["text"])
                yield {"type": "chunk", "provider": provider, "chunk": event["text"]}
            elif "usage" in event:
                usage = event["usage"]
    except Exception as exc:
        _log.warning("%s (%s) stream failed: %s", provider, version.model_id, exc)
        yield {"type": "chunk", "provider": provider, "chunk": f"\n\n[{provider} error: {exc}]"}
        yield {
            "type": "run_completed",
            "provider": provider,
            "model_id": version.model_id,
            "status": "error",
            "metrics": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                        "latency_ms": int((time.monotonic() - started) * 1000)},
        }
        return
    tokens_in = usage["in"] if usage else prompt_chars // 4
    tokens_out = usage["out"] if usage else out_chars // 4
    yield {
        "type": "run_completed",
        "provider": provider,
        "model_id": version.model_id,
        "status": "ok",
        "metrics": {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_estimated": usage is None,
            "cost_usd": model_registry.estimate_cost_usd(version, tokens_in, tokens_out),
            "latency_ms": int((time.monotonic() - started) * 1000),
        },
    }


async def synthesize_parallel_stream(
    messages: list[dict],
    selections: dict[str, str | None] | None = None,
) -> AsyncGenerator[str, None]:
    """Run the enabled providers concurrently and yield SSE lines.

    ``selections`` maps provider -> requested model_id (or None for the
    default). When given, ONLY those providers run; unknown/unconfigured
    entries are dropped. When omitted, every configured provider runs at
    its default version.
    """
    configured = get_configured_providers()
    if selections is None:
        selections = {p: None for p in configured}

    runs: list[tuple[str, model_registry.ModelVersion]] = []
    for provider, model_id in selections.items():
        if provider not in configured:
            continue
        version = model_registry.resolve(provider, model_id, configured)
        if version is not None:
            runs.append((provider, version))

    if not runs:
        msg = (
            "No LLM provider is currently configured. Please provide your API keys in `.env`:\n\n"
            "- `DOCVAULT_OPENAI_API_KEY` (for OpenAI)\n"
            "- `DOCVAULT_ANTHROPIC_API_KEY` (for Claude)\n"
            "- `DOCVAULT_GEMINI_API_KEY` (for Gemini)\n"
            "- `DOCVAULT_VLLM_URL` (for local vLLM)\n"
        )
        yield f"data: {json.dumps({'type': 'chunk', 'provider': 'Notice', 'chunk': msg})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    queue: asyncio.Queue = asyncio.Queue()

    async def producer(provider: str, version: model_registry.ModelVersion) -> None:
        try:
            async for event in _run_model(provider, version, messages):
                await queue.put(f"data: {json.dumps(event)}\n\n")
        except Exception as exc:  # defensive: never wedge the stream
            _log.error("Producer exception for %s: %s", provider, exc)
        finally:
            await queue.put(None)

    tasks = [asyncio.create_task(producer(p, v)) for p, v in runs]
    active = len(tasks)
    while active > 0:
        item = await queue.get()
        if item is None:
            active -= 1
        else:
            yield item
    yield f"data: {json.dumps({'type': 'done'})}\n\n"
