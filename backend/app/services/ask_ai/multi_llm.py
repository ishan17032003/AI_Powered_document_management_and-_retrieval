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
import re
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
                        entry = tool_calls.setdefault(tc.index, {"id": "", "name": "", "args": "", "announced": False})
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function and tc.function.name:
                            entry["name"] = tc.function.name
                        if entry["name"] and not entry["announced"]:
                            entry["announced"] = True
                            yield {"tool": {"event": "started", "tool": entry["name"],
                                            "label": tool_defs.TOOL_LABELS.get(entry["name"], entry["name"]),
                                            "args_summary": "composing…"}}
                        if tc.function and tc.function.arguments:
                            entry["args"] += tc.function.arguments
                            if entry["announced"]:
                                yield {"tool_progress": {"tool": entry["name"], "text": tc.function.arguments}}
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
                if not call.get("announced"):
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
    from anthropic import AsyncAnthropic, NotFoundError

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
    effective_model = model_id or "claude-haiku-4-5-20251001"

    for round_no in range(_MAX_TOOL_ROUNDS + 1):
        budget = _THINKING_BUDGETS.get(reasoning or "")
        is_adaptive = any(x in effective_model for x in ["-5", "-4-8", "-4-7", "-4-6"]) and "haiku" not in effective_model and "-4-5" not in effective_model

        request: dict = {
            "max_tokens": (budget + 2048) if (budget and not is_adaptive) else 2048,
            "system": system,
            "messages": anthropic_messages,
            "model": effective_model,
        }
        if reasoning in ("low", "medium", "high"):
            if is_adaptive:
                request["thinking"] = {"type": "adaptive"}
                request["output_config"] = {"effort": reasoning}
            elif budget:
                request["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if tool_schemas and round_no < _MAX_TOOL_ROUNDS:
            request["tools"] = tool_schemas
        
        try:
            stream_ctx = client.messages.stream(**request)
            async with stream_ctx as stream:
                async for text in stream.text_stream:
                    yield {"text": text}
                final = await stream.get_final_message()
        except NotFoundError as err:
            if effective_model != "claude-haiku-4-5-20251001":
                _log.warning("Anthropic model %s not found (404), retrying with claude-haiku-4-5-20251001: %s", effective_model, err)
                effective_model = "claude-haiku-4-5-20251001"
                request["model"] = effective_model
                async with client.messages.stream(**request) as stream:
                    async for text in stream.text_stream:
                        yield {"text": text}
                    final = await stream.get_final_message()
            else:
                raise
        except Exception as exc:
            err_str = str(exc).lower()
            if "thinking" in err_str or "adaptive" in err_str or "output_config" in err_str:
                _log.warning("Anthropic thinking error on model %s (%s), retrying with adjusted thinking config", effective_model, exc)
                # If adaptive was used, try budget enabled
                if is_adaptive and budget:
                    request["thinking"] = {"type": "enabled", "budget_tokens": budget}
                    request.pop("output_config", None)
                    request["max_tokens"] = budget + 2048
                elif not is_adaptive and reasoning in ("low", "medium", "high"):
                    request["thinking"] = {"type": "adaptive"}
                    request["output_config"] = {"effort": reasoning}
                    request["max_tokens"] = 2048
                else:
                    request.pop("thinking", None)
                    request.pop("output_config", None)
                
                try:
                    async with client.messages.stream(**request) as stream:
                        async for text in stream.text_stream:
                            yield {"text": text}
                        final = await stream.get_final_message()
                except Exception as retry_exc:
                    # Final fallback: retry with thinking completely removed
                    _log.warning("Anthropic retry failed (%s), retrying plain without thinking", retry_exc)
                    request.pop("thinking", None)
                    request.pop("output_config", None)
                    async with client.messages.stream(**request) as stream:
                        async for text in stream.text_stream:
                            yield {"text": text}
                        final = await stream.get_final_message()
            else:
                raise

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


async def _stream_gemini(messages, api_key: str, model_id: str, reasoning: str | None = None):
    """Direct HTTP SSE streaming for Google Gemini models."""
    clean_model_id = model_id.replace("models/", "") if model_id else "gemini-2.5-flash"

    contents = []
    system_instruction = None
    for m in messages:
        if m["role"] == "system":
            system_instruction = {"parts": [{"text": m["content"]}]}
        elif m["role"] == "user":
            contents.append({"role": "user", "parts": [{"text": m["content"]}]})
        elif m["role"] == "assistant":
            contents.append({"role": "model", "parts": [{"text": m["content"]}]})

    payload: dict = {"contents": contents}
    if system_instruction:
        payload["system_instruction"] = system_instruction

    gen_config: dict = {}
    if reasoning in ("low", "medium", "high"):
        budget_map = {"low": 2048, "medium": 8192, "high": 16384}
        gen_config["thinking_config"] = {"thinking_budget": budget_map.get(reasoning, 2048)}
    if gen_config:
        payload["generation_config"] = gen_config

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model_id}:streamGenerateContent?alt=sse&key={api_key}"

    usage_acc = {"in": 0, "out": 0}
    got_usage = False

    async with httpx.AsyncClient(timeout=35.0) as client:
        try:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    err_body = await response.aread()
                    err_text = err_body.decode("utf-8", errors="replace")
                    # Auto fallback to gemini-2.5-flash if custom/preview model failed
                    if clean_model_id != "gemini-2.5-flash":
                        _log.warning("Gemini model %s returned %s (%s), falling back to gemini-2.5-flash", clean_model_id, response.status_code, err_text[:100])
                        fb_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse&key={api_key}"
                        payload.pop("generation_config", None)
                        async with client.stream("POST", fb_url, json=payload) as fb_res:
                            if fb_res.status_code == 200:
                                async for line in fb_res.aiter_lines():
                                    if line.startswith("data: "):
                                        try:
                                            chunk_data = json.loads(line[6:])
                                            candidates = chunk_data.get("candidates", [])
                                            if candidates:
                                                parts = candidates[0].get("content", {}).get("parts", [])
                                                for p in parts:
                                                    if "text" in p and p["text"]:
                                                        yield {"text": p["text"]}
                                            meta = chunk_data.get("usageMetadata")
                                            if meta:
                                                usage_acc["in"] = meta.get("promptTokenCount", 0) or usage_acc["in"]
                                                usage_acc["out"] = meta.get("candidatesTokenCount", 0) or usage_acc["out"]
                                                got_usage = True
                                        except Exception:
                                            pass
                                if got_usage:
                                    yield {"usage": usage_acc}
                                return
                    raise RuntimeError(f"Gemini API error ({response.status_code}): {err_text[:200]}")

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            chunk_data = json.loads(line[6:])
                            candidates = chunk_data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                for p in parts:
                                    if "text" in p and p["text"]:
                                        yield {"text": p["text"]}
                            meta = chunk_data.get("usageMetadata")
                            if meta:
                                usage_acc["in"] = meta.get("promptTokenCount", 0) or usage_acc["in"]
                                usage_acc["out"] = meta.get("candidatesTokenCount", 0) or usage_acc["out"]
                                got_usage = True
                        except Exception:
                            pass
        except Exception as exc:
            _log.warning("Gemini stream exception: %s", exc)
            raise

    if got_usage:
        yield {"usage": usage_acc}


async def _stream_openai_responses(messages, model_id: str, tools_ctx, *, api_key: str, capabilities: tuple[str, ...] = (), provider: str = "openai", reasoning: str | None = None):
    """GPT-5.x via /v1/responses — the only OpenAI surface where function
    tools and reasoning work together (chat completions 400s on that combo)."""
    from openai import AsyncOpenAI

    from . import tools as tool_defs

    client = AsyncOpenAI(api_key=api_key)
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    input_items: list = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m["role"] != "system"
    ]
    flat_tools = [
        {
            "type": "function",
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": t["function"]["parameters"],
        }
        for t in tool_defs.openai_tool_schemas(capabilities)
    ] if tools_ctx is not None else None

    usage_acc = {"in": 0, "out": 0}
    got_usage = False
    previous_response_id: str | None = None

    for round_no in range(_MAX_TOOL_ROUNDS + 1):
        kwargs: dict = {"model": model_id, "stream": True}
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        kwargs["input"] = input_items
        if system:
            kwargs["instructions"] = system
        if reasoning not in (None, "", "none"):
            kwargs["reasoning"] = {"effort": reasoning}
        if flat_tools and round_no < _MAX_TOOL_ROUNDS:
            kwargs["tools"] = flat_tools
        stream = await client.responses.create(**kwargs)

        calls: list = []
        response_id = None
        announced_calls: set = set()
        async for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                yield {"text": event.delta}
            elif etype == "response.output_item.added":
                item = getattr(event, "item", None)
                if item is not None and getattr(item, "type", "") == "function_call" and getattr(item, "name", ""):
                    announced_calls.add(item.name)
                    yield {"tool": {"event": "started", "tool": item.name,
                                    "label": tool_defs.TOOL_LABELS.get(item.name, item.name),
                                    "args_summary": "composing…"}}
            elif etype == "response.function_call_arguments.delta":
                if announced_calls:
                    yield {"tool_progress": {"tool": next(iter(announced_calls)), "text": getattr(event, "delta", "") or ""}}
            elif etype == "response.completed":
                resp = event.response
                response_id = resp.id
                usage = getattr(resp, "usage", None)
                if usage:
                    usage_acc["in"] += getattr(usage, "input_tokens", 0) or 0
                    usage_acc["out"] += getattr(usage, "output_tokens", 0) or 0
                    got_usage = True
                calls = [o for o in (resp.output or []) if getattr(o, "type", "") == "function_call"]

        if calls and tools_ctx is not None:
            previous_response_id = response_id
            input_items = []
            for call in calls:
                label = tool_defs.TOOL_LABELS.get(call.name, call.name)
                if call.name not in announced_calls:
                    yield {"tool": {"event": "started", "tool": call.name, "label": label,
                                    "args_summary": (call.arguments or "")[:160]}}
                result = await tools_ctx.run(call.name, call.arguments or "{}", provider=provider)
                yield {"tool": {"event": "result", "tool": call.name, "label": label,
                                "summary": tool_defs.summarize_result(call.name, result),
                                "status": "error" if "error" in result else "ok"}}
                for art in result.get("_artifacts", []):
                    yield {"artifact": art}
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": tool_defs.clip_result_json(result),
                })
            continue
        break

    if got_usage:
        yield {"usage": usage_acc}


_THOUGHT_RE = re.compile(r"(?i)^\s*(?:thought|thinking)\s*:?\s*\n")

_TOOL_CALL_PREFIX = "TOOL_CALL:"
# Models get the format wrong in every way imaginable: tool_call:, Tool Call:,
# CALL:search_documents inside fences… match tolerantly, case-insensitively.
# Anchored to a known tool name (or the legacy JSON form) so ordinary prose
# containing the word "call:" is never swallowed as a tool call.
_TOOL_CALL_RE = re.compile(
    r"(?i)(?:tool[_ ]?)?call\s*:\s*"
    r"(?=\{|\"?(?:execute_python|search_documents|list_documents)\b)"
)


def _prompted_tools_instructions(schemas: list[dict]) -> str:
    lines = [
        "\n\nTOOLS AVAILABLE. To call a tool, your ENTIRE reply must be:",
        "TOOL_CALL: <tool_name>",
        '{"<arg>": <value>, ...}',
        "For execute_python, instead of JSON put the code in a fenced block:",
        "TOOL_CALL: execute_python",
        "```python",
        "<your code>",
        "```",
        "You will receive the result as a message starting with TOOL_RESULT, then continue.",
        "When you can answer, reply normally WITHOUT the TOOL_CALL prefix. Tools:",
    ]
    for t in schemas:
        fn = t["function"]
        lines.append(f'- {fn["name"]}: {fn["description"]} Parameters JSON schema: {json.dumps(fn["parameters"])}')
    return "\n".join(lines)


def _parse_prompted_tool_call(full: str) -> tuple[str, str] | None:
    """Parse 'TOOL_CALL: name' + JSON args or fenced code. Returns (name, args_json)."""
    text = full.lstrip()
    m = _TOOL_CALL_RE.match(text)
    if m is None:
        return None
    first_line, _, rest = text[m.end():].partition("\n")
    name = first_line.strip().strip('"{}:,')
    rest = rest.strip()
    # Legacy single-line form: TOOL_CALL: {"name": ..., "arguments": {...}}
    if not name and text[m.end():].lstrip().startswith("{"):
        raw = text[m.end():].lstrip()
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw)
            if isinstance(payload, dict) and payload.get("name"):
                return str(payload["name"]), json.dumps(payload.get("arguments") or {})
        except (json.JSONDecodeError, ValueError):
            return None
    if not name:
        return None
    # Fenced code block → execute_python style args
    if "```" in rest:
        fence = rest.split("```", 2)
        if len(fence) >= 2:
            code = fence[1]
            if code.startswith(("python\n", "python\r\n")):
                code = code.partition("\n")[2]
            elif code.startswith("python"):
                code = code[len("python"):]
            return name, json.dumps({"code": code.strip("\n")})
    if rest.startswith("{"):
        try:
            payload, _ = json.JSONDecoder().raw_decode(rest)
            if isinstance(payload, dict):
                if "name" in payload and "arguments" in payload:
                    return str(payload["name"]) or name, json.dumps(payload.get("arguments") or {})
                return name, json.dumps(payload)
        except (json.JSONDecodeError, ValueError):
            pass
    if not rest:
        return name, "{}"
    return None


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
        # Detect TOOL_CALL anywhere in the stream: emit visible text with a
        # small holdback so the marker never leaks to the card, then swallow
        # everything from the marker onward as the tool call.
        full = ""
        streamed = 0
        is_tool = False
        tool_start = -1
        announced_tool = None  # tool_started emitted early, as soon as the name is known
        holdback = len(_TOOL_CALL_PREFIX) + 8
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
            if is_tool:
                continue
            match = _TOOL_CALL_RE.search(full)
            if match is not None:
                is_tool = True
                tool_start = match.start()
                tool_body_at = match.end()
                # Emit clean preamble (drop an opening code fence before the call).
                preamble = full[streamed:tool_start].rstrip()
                if streamed == 0:
                    preamble = _THOUGHT_RE.sub("", preamble)
                if preamble.endswith("```python"):
                    preamble = preamble[: -len("```python")].rstrip()
                if preamble.endswith("```"):
                    preamble = preamble[:-3].rstrip()
                if preamble:
                    yield {"text": preamble}
                streamed = len(full)
            else:
                safe = len(full) - holdback
                if safe > streamed:
                    if streamed == 0:
                        # Hold the very first flush until a newline (or enough
                        # text) so a leading gemma "thought" line can be dropped.
                        if "\n" not in full[:safe] and safe < 60:
                            continue
                        head = _THOUGHT_RE.sub("", full[:safe])
                        if head:
                            yield {"text": head}
                        streamed = safe
                    else:
                        yield {"text": full[streamed:safe]}
                        streamed = safe
            if is_tool and announced_tool is None:
                # Announce the tool row live so the card shows progress while
                # the model is still streaming the (hidden) code block.
                first_line, nl, _ = full[tool_body_at:].partition("\n")
                if nl:
                    name = first_line.strip().strip('"{}:,')
                    if name:
                        announced_tool = name
                        progress_sent = len(full)
                        yield {"tool": {"event": "started", "tool": name,
                                        "label": tool_defs.TOOL_LABELS.get(name, name),
                                        "args_summary": "composing…"}}
            elif is_tool and announced_tool is not None:
                if len(full) > progress_sent:
                    yield {"tool_progress": {"tool": announced_tool, "text": full[progress_sent:]}}
                    progress_sent = len(full)
        if not is_tool and streamed < len(full):
            tail = full[streamed:]
            if streamed == 0:
                tail = _THOUGHT_RE.sub("", tail)
            yield {"text": tail}
        if not is_tool:
            break
        parsed = _parse_prompted_tool_call(full[tool_start:])
        if parsed is None:
            _log.warning("vllm prompted tool call unparseable: %r", full[:300])
            msgs.append({"role": "assistant", "content": full})
            msgs.append({"role": "user",
                         "content": "Your TOOL_CALL could not be parsed. Follow the exact format: "
                                    "first line 'TOOL_CALL: <name>', then JSON arguments on the next "
                                    "line, or a ```python fenced block for execute_python. Try again "
                                    "or answer directly."})
            continue
        call_name, call_args = parsed
        label = tool_defs.TOOL_LABELS.get(call_name, call_name)
        if announced_tool != call_name:
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
    "openai": lambda messages, model_id, tools_ctx, caps, reasoning: (
        _stream_openai_responses(
            messages, model_id, tools_ctx,
            api_key=_key("openai_api_key", "DOCVAULT_OPENAI_API_KEY", "OPENAI_API_KEY"),
            capabilities=caps, provider="openai", reasoning=reasoning)
        if model_id.startswith(("gpt-5", "o1", "o3", "o4"))
        else _stream_openai_compatible(
            messages, model_id, tools_ctx,
            api_key=_key("openai_api_key", "DOCVAULT_OPENAI_API_KEY", "OPENAI_API_KEY"),
            capabilities=caps, provider="openai", reasoning=reasoning)),
    "claude": lambda messages, model_id, tools_ctx, caps, reasoning: _stream_anthropic(
        messages, _key("anthropic_api_key", "DOCVAULT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"), model_id, tools_ctx, capabilities=caps, provider="claude", reasoning=reasoning),
    "gemini": lambda messages, model_id, tools_ctx, caps, reasoning: _stream_gemini(
        messages, _key("gemini_api_key", "DOCVAULT_GEMINI_API_KEY", "GEMINI_API_KEY"), model_id, reasoning=reasoning),
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
            elif "tool_progress" in event:
                yield {"type": "tool_progress", "provider": provider, **event["tool_progress"]}
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
