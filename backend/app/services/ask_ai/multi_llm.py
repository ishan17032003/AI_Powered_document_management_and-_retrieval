"""Parallel multi-model streaming for Ask AI.

Each configured provider runs concurrently against the identical prompt.
Per run the stream emits, as SSE ``data:`` lines:

  {"type": "run_started",   "provider", "model_id", "display_version"}
  {"type": "chunk",         "provider", "chunk"}                (many)
  {"type": "tool_started",  "provider", "tool", "label", "args_summary"}
  {"type": "tool_result",   "provider", "tool", "label", "summary", "status"}
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


_MAX_TOOL_ROUNDS = 4


def _is_reasoning_model(model_id: str) -> bool:
    return model_id.startswith(("o1", "o3", "o4", "gpt-5"))


async def _stream_openai_compatible(messages, model_id: str, tools_ctx, *, api_key: str | None = None, base_url: str | None = None, capabilities: tuple[str, ...] = (), provider: str = "", reasoning: str | None = None):
    """OpenAI-compatible tool-use loop (OpenAI itself and vLLM)."""
    from openai import AsyncOpenAI

    from . import tools as tool_defs

    kwargs: dict = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
        kwargs.setdefault("api_key", "not-needed")
    client = AsyncOpenAI(**kwargs)

    msgs = list(messages)
    tool_schemas = tool_defs.openai_tool_schemas(capabilities) if tools_ctx is not None else None
    usage_acc = {"in": 0, "out": 0}
    got_usage = False

    for round_no in range(_MAX_TOOL_ROUNDS + 1):
        use_tools = tool_schemas if (tool_schemas and round_no < _MAX_TOOL_ROUNDS) else None
        request: dict = {
            "model": model_id,
            "messages": msgs,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if not _is_reasoning_model(model_id):
            request["temperature"] = 0.2
        wants_reasoning = reasoning not in (None, "", "none")
        if model_id.startswith("gpt-5"):
            if wants_reasoning:
                # GPT-5.x chat completions cannot combine function tools with
                # reasoning — the user's explicit reasoning choice wins and the
                # run goes tool-less.
                use_tools = None
                request["reasoning_effort"] = reasoning
            elif use_tools:
                request["reasoning_effort"] = "none"
        elif wants_reasoning and model_id.startswith(("o1", "o3", "o4")):
            request["reasoning_effort"] = reasoning
        if use_tools:
            request["tools"] = use_tools
        try:
            stream = await client.chat.completions.create(**request)
        except Exception as exc:
            if use_tools:
                # Backend without tool support (e.g. a vLLM build) — degrade to
                # text, but never silently: this cost us the sandbox once.
                _log.warning(
                    "%s: tools request rejected (%s: %s) — retrying text-only",
                    model_id, type(exc).__name__, str(exc)[:200],
                )
                tool_schemas = None
                request.pop("tools", None)
                request.pop("reasoning_effort", None)
                stream = await client.chat.completions.create(**request)
            else:
                raise

        tool_calls: dict[int, dict] = {}
        finish_reason = None
        async for chunk in stream:
            if chunk.choices:
                choice = chunk.choices[0]
                delta = choice.delta
                if delta and delta.content:
                    yield {"text": delta.content}
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        entry = tool_calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function and tc.function.name:
                            entry["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            entry["args"] += tc.function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
            usage = getattr(chunk, "usage", None)
            if usage:
                usage_acc["in"] += usage.prompt_tokens or 0
                usage_acc["out"] += usage.completion_tokens or 0
                got_usage = True

        if finish_reason == "tool_calls" and tool_calls and tools_ctx is not None:
            ordered = [tool_calls[i] for i in sorted(tool_calls)]
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": c["id"] or f"call_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for i, c in enumerate(ordered)
                ],
            })
            for i, call in enumerate(ordered):
                label = tool_defs.TOOL_LABELS.get(call["name"], call["name"])
                yield {"tool": {"event": "started", "tool": call["name"], "label": label,
                                "args_summary": (call["args"] or "")[:160]}}
                result = await tools_ctx.run(call["name"], call["args"] or "{}", provider=provider)
                yield {"tool": {"event": "result", "tool": call["name"], "label": label,
                                "summary": tool_defs.summarize_result(call["name"], result),
                                "status": "error" if "error" in result else "ok"}}
                for art in result.get("_artifacts", []):
                    yield {"artifact": art}
                msgs.append({
                    "role": "tool",
                    "tool_call_id": call["id"] or f"call_{i}",
                    "content": tool_defs.clip_result_json(result),
                })
            continue
        break

    if got_usage:
        yield {"usage": usage_acc}


_THINKING_BUDGETS = {"low": 2048, "medium": 8192, "high": 16384}


async def _stream_anthropic(messages, api_key: str, model_id: str, tools_ctx=None, capabilities: tuple[str, ...] = (), provider: str = "", reasoning: str | None = None):
    from anthropic import AsyncAnthropic

    from . import tools as tool_defs

    client = AsyncAnthropic(api_key=api_key)
    system = ""
    anthropic_messages = []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        else:
            anthropic_messages.append({"role": m["role"], "content": m["content"]})

    tool_schemas = tool_defs.anthropic_tool_schemas(capabilities) if tools_ctx is not None else None
    usage_acc = {"in": 0, "out": 0}
    got_usage = False

    for round_no in range(_MAX_TOOL_ROUNDS + 1):
        budget = _THINKING_BUDGETS.get(reasoning or "")
        request: dict = {
            "max_tokens": (budget + 2048) if budget else 1024,
            "system": system,
            "messages": anthropic_messages,
            "model": model_id,
        }
        if budget:
            # Extended thinking: temperature must stay unset and max_tokens
            # must exceed the thinking budget.
            request["thinking"] = {"type": "enabled", "budget_tokens": budget}
        else:
            request["temperature"] = 0.2
        if tool_schemas and round_no < _MAX_TOOL_ROUNDS:
            request["tools"] = tool_schemas
        async with client.messages.stream(**request) as stream:
            async for text in stream.text_stream:
                yield {"text": text}
            final = await stream.get_final_message()
        if final and final.usage:
            usage_acc["in"] += final.usage.input_tokens or 0
            usage_acc["out"] += final.usage.output_tokens or 0
            got_usage = True
        tool_uses = [b for b in (final.content if final else []) if getattr(b, "type", "") == "tool_use"]
        if tool_uses and tools_ctx is not None:
            anthropic_messages.append({"role": "assistant", "content": final.content})
            results = []
            for block in tool_uses:
                label = tool_defs.TOOL_LABELS.get(block.name, block.name)
                args_json = json.dumps(block.input or {})
                yield {"tool": {"event": "started", "tool": block.name, "label": label,
                                "args_summary": args_json[:160]}}
                result = await tools_ctx.run(block.name, args_json, provider=provider)
                yield {"tool": {"event": "result", "tool": block.name, "label": label,
                                "summary": tool_defs.summarize_result(block.name, result),
                                "status": "error" if "error" in result else "ok"}}
                for art in result.get("_artifacts", []):
                    yield {"artifact": art}
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": tool_defs.clip_result_json(result)})
            anthropic_messages.append({"role": "user", "content": results})
            continue
        break

    if got_usage:
        yield {"usage": usage_acc}


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


_TOOL_CALL_PREFIX = "TOOL_CALL:"


def _prompted_tools_instructions(schemas: list[dict]) -> str:
    lines = [
        "\n\nTOOLS AVAILABLE. To call a tool, reply with ONLY one line and nothing else:",
        'TOOL_CALL: {"name": "<tool_name>", "arguments": {...}}',
        "You will receive the result as a message starting with TOOL_RESULT, then continue.",
        "When you can answer, reply normally WITHOUT the TOOL_CALL prefix. Tools:",
    ]
    for t in schemas:
        fn = t["function"]
        lines.append(f'- {fn["name"]}: {fn["description"]} Parameters JSON schema: {json.dumps(fn["parameters"])}')
    return "\n".join(lines)


async def _stream_vllm_prompted_tools(messages, model_id: str, tools_ctx, *, base_url: str, capabilities: tuple[str, ...] = (), provider: str = "vllm"):
    """Tool use for OpenAI-compatible servers WITHOUT a tool-call parser.

    The remote vLLM (no --enable-auto-tool-choice) rejects the tools param, so
    tools are offered through the prompt and calls are parsed from a
    TOOL_CALL: {...} line. Plain answers stream through untouched.
    """
    from openai import AsyncOpenAI

    from . import tools as tool_defs

    if tools_ctx is None:
        async for event in _stream_openai_compatible(messages, model_id, None, base_url=base_url, provider=provider):
            yield event
        return

    client = AsyncOpenAI(base_url=base_url, api_key="not-needed")
    schemas = tool_defs.openai_tool_schemas(capabilities)
    msgs = [dict(m) for m in messages]
    for m in msgs:
        if m["role"] == "system":
            m["content"] = m["content"] + _prompted_tools_instructions(schemas)
            break
    usage_acc = {"in": 0, "out": 0}
    got_usage = False

    for _round in range(_MAX_TOOL_ROUNDS + 1):
        stream = await client.chat.completions.create(
            model=model_id, messages=msgs, stream=True, temperature=0.2,
            max_tokens=1024, stream_options={"include_usage": True},
        )
        buffer = ""
        decided = False
        is_tool = False
        full = ""
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                usage_acc["in"] += usage.prompt_tokens or 0
                usage_acc["out"] += usage.completion_tokens or 0
                got_usage = True
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content or ""
            if not text:
                continue
            full += text
            if not decided:
                buffer += text
                probe = buffer.lstrip()
                if len(probe) >= len(_TOOL_CALL_PREFIX) or "\n" in buffer:
                    decided = True
                    is_tool = probe.startswith(_TOOL_CALL_PREFIX)
                    if not is_tool:
                        yield {"text": buffer}
            elif not is_tool:
                yield {"text": text}
        if not decided and buffer:
            is_tool = buffer.lstrip().startswith(_TOOL_CALL_PREFIX)
            if not is_tool:
                yield {"text": buffer}
        if not is_tool:
            break
        # Parse the tool call
        raw = full.lstrip()[len(_TOOL_CALL_PREFIX):].strip()
        start = raw.find("{")
        call_name, call_args = "", "{}"
        if start >= 0:
            payload = None
            for candidate in (raw[start:], raw[start:raw.rfind("}") + 1]):
                try:
                    payload = json.loads(candidate)
                    break
                except (json.JSONDecodeError, ValueError):
                    continue
            if isinstance(payload, dict):
                call_name = str(payload.get("name", ""))
                call_args = json.dumps(payload.get("arguments") or {})
        if not call_name:
            yield {"text": "\n[tool call could not be parsed]"}
            break
        label = tool_defs.TOOL_LABELS.get(call_name, call_name)
        yield {"tool": {"event": "started", "tool": call_name, "label": label,
                        "args_summary": call_args[:160]}}
        result = await tools_ctx.run(call_name, call_args, provider=provider)
        yield {"tool": {"event": "result", "tool": call_name, "label": label,
                        "summary": tool_defs.summarize_result(call_name, result),
                        "status": "error" if "error" in result else "ok"}}
        for art in result.get("_artifacts", []):
            yield {"artifact": art}
        msgs.append({"role": "assistant", "content": full})
        msgs.append({"role": "user",
                     "content": f"TOOL_RESULT {call_name}: {tool_defs.clip_result_json(result)}\n"
                                "Continue. Answer the user now unless another tool call is required."})

    if got_usage:
        yield {"usage": usage_acc}


_STREAMS = {
    "openai": lambda messages, model_id, tools_ctx, caps, reasoning: _stream_openai_compatible(
        messages, model_id, tools_ctx,
        api_key=_key("openai_api_key", "DOCVAULT_OPENAI_API_KEY", "OPENAI_API_KEY"), capabilities=caps, provider="openai", reasoning=reasoning),
    "claude": lambda messages, model_id, tools_ctx, caps, reasoning: _stream_anthropic(
        messages, _key("anthropic_api_key", "DOCVAULT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"), model_id, tools_ctx, capabilities=caps, provider="claude", reasoning=reasoning),
    # Gemini runs text-only in Phase 2 (function-calling and thinking-budget
    # wiring land with the google-genai SDK migration).
    "gemini": lambda messages, model_id, tools_ctx, caps, reasoning: _stream_gemini(
        messages, _key("gemini_api_key", "DOCVAULT_GEMINI_API_KEY", "GEMINI_API_KEY"), model_id),
    "vllm": lambda messages, model_id, tools_ctx, caps, reasoning: _stream_vllm_prompted_tools(
        messages, model_id, tools_ctx,
        base_url=_key("vllm_url", "DOCVAULT_VLLM_URL", "VLLM_URL"), capabilities=caps),
}


async def _run_model(
    provider: str,
    version: model_registry.ModelVersion,
    messages: list[dict],
    tools_ctx=None,
    reasoning: str | None = None,
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
        "reasoning": reasoning or "none",
    }
    try:
        if reasoning not in (None, "", "none") and reasoning not in version.reasoning_levels:
            reasoning = None
        async for event in _STREAMS[provider](messages, version.model_id, tools_ctx, version.capabilities, reasoning):
            if "text" in event:
                out_chars += len(event["text"])
                yield {"type": "chunk", "provider": provider, "chunk": event["text"]}
            elif "artifact" in event:
                yield {"type": "artifact", "provider": provider, **event["artifact"]}
            elif "tool" in event:
                info = event["tool"]
                etype = "tool_started" if info.get("event") == "started" else "tool_result"
                yield {"type": etype, "provider": provider, **{k: v for k, v in info.items() if k != "event"}}
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
    selections: dict[str, dict | str | None] | None = None,
    tools_ctx=None,
) -> AsyncGenerator[str, None]:
    """Run the enabled providers concurrently and yield SSE lines.

    ``selections`` maps provider -> {"model_id": ..., "reasoning": ...}
    (either value may be None). When given, ONLY those providers run;
    unknown/unconfigured entries are dropped. When omitted, every configured
    provider runs at its default version with reasoning off.
    """
    configured = get_configured_providers()
    if selections is None:
        selections = {p: {} for p in configured}

    runs: list[tuple[str, model_registry.ModelVersion, str | None]] = []
    for provider, choice in selections.items():
        if provider not in configured:
            continue
        if choice is None or isinstance(choice, str):
            choice = {"model_id": choice}
        version = model_registry.resolve(provider, choice.get("model_id"), configured)
        if version is not None:
            runs.append((provider, version, choice.get("reasoning")))

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

    async def producer(provider: str, version: model_registry.ModelVersion, reasoning: str | None) -> None:
        try:
            async for event in _run_model(provider, version, messages, tools_ctx, reasoning):
                await queue.put(f"data: {json.dumps(event)}\n\n")
        except Exception as exc:  # defensive: never wedge the stream
            _log.error("Producer exception for %s: %s", provider, exc)
        finally:
            await queue.put(None)

    tasks = [asyncio.create_task(producer(p, v, r)) for p, v, r in runs]
    active = len(tasks)
    while active > 0:
        item = await queue.get()
        if item is None:
            active -= 1
        else:
            yield item
    yield f"data: {json.dumps({'type': 'done'})}\n\n"
