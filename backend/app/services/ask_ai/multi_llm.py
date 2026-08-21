import asyncio
import json
import logging
import os
from typing import AsyncGenerator

import httpx
from ...config import settings

_log = logging.getLogger(__name__)


def get_configured_providers() -> list[str]:
    openai_key = settings.openai_api_key or os.getenv("DOCVAULT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    anthropic_key = settings.anthropic_api_key or os.getenv("DOCVAULT_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""
    gemini_key = settings.gemini_api_key or os.getenv("DOCVAULT_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    vllm_url = settings.vllm_url or os.getenv("DOCVAULT_VLLM_URL") or os.getenv("VLLM_URL") or ""

    providers = []
    if openai_key.strip():
        providers.append("openai")
    if anthropic_key.strip():
        providers.append("claude")
    if gemini_key.strip():
        providers.append("gemini")
    if vllm_url.strip():
        providers.append("vllm")
    return providers


async def _stream_openai(messages: list[dict], api_key: str) -> AsyncGenerator[str, None]:
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        stream = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            stream=True,
            temperature=0.2,
        )
        async for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                content = chunk.choices[0].delta.content
                if content:
                    yield json.dumps({"type": "chunk", "provider": "openai", "chunk": content})
    except Exception as exc:
        _log.warning(f"OpenAI stream failed: {exc}")
        yield json.dumps({"type": "chunk", "provider": "openai", "chunk": f"\n\n[OpenAI Error: {exc}]"})


async def _stream_anthropic(messages: list[dict], api_key: str) -> AsyncGenerator[str, None]:
    try:
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
            model="claude-3-haiku-20240307",
            temperature=0.2,
        ) as stream:
            async for text in stream.text_stream:
                yield json.dumps({"type": "chunk", "provider": "claude", "chunk": text})
    except Exception as exc:
        _log.warning(f"Anthropic stream failed: {exc}")
        yield json.dumps({"type": "chunk", "provider": "claude", "chunk": f"\n\n[Claude Error: {exc}]"})


async def _stream_gemini(messages: list[dict], api_key: str) -> AsyncGenerator[str, None]:
    try:
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
        
        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=system)
        response = await model.generate_content_async(gemini_messages, stream=True)
        async for chunk in response:
            if chunk.text:
                yield json.dumps({"type": "chunk", "provider": "gemini", "chunk": chunk.text})
    except Exception as exc:
        _log.warning(f"Gemini stream failed: {exc}")
        yield json.dumps({"type": "chunk", "provider": "gemini", "chunk": f"\n\n[Gemini Error: {exc}]"})


async def _stream_vllm(messages: list[dict], vllm_url: str) -> AsyncGenerator[str, None]:
    try:
        url = f"{vllm_url.rstrip('/')}/chat/completions"
        payload = {
            "model": settings.vllm_model or "gemma-4-31b",
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
            "max_tokens": 1024,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", url, json=payload) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            choices = data.get("choices")
                            if choices and len(choices) > 0:
                                content = choices[0].get("delta", {}).get("content", "")
                                if content:
                                    yield json.dumps({"type": "chunk", "provider": "vllm", "chunk": content})
                        except json.JSONDecodeError:
                            continue
    except Exception as exc:
        _log.warning(f"vLLM stream failed: {exc}")
        yield json.dumps({"type": "chunk", "provider": "vllm", "chunk": f"\n\n[vLLM Error: {exc}]"})


async def synthesize_parallel_stream(messages: list[dict]) -> AsyncGenerator[str, None]:
    """Spawns concurrent LLM streams ONLY for available/configured providers and yields SSE chunks."""
    queue = asyncio.Queue()

    async def producer(gen):
        try:
            async for chunk_data in gen:
                await queue.put(f"data: {chunk_data}\n\n")
        except Exception as e:
            _log.error(f"Producer exception: {e}")
        finally:
            await queue.put(None)  # Sentinel

    openai_key = settings.openai_api_key or os.getenv("DOCVAULT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    anthropic_key = settings.anthropic_api_key or os.getenv("DOCVAULT_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""
    gemini_key = settings.gemini_api_key or os.getenv("DOCVAULT_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    vllm_url = settings.vllm_url or os.getenv("DOCVAULT_VLLM_URL") or os.getenv("VLLM_URL") or ""

    tasks = []
    
    # 1. OpenAI
    if openai_key.strip():
        tasks.append(asyncio.create_task(producer(_stream_openai(messages, openai_key.strip()))))
        
    # 2. Anthropic
    if anthropic_key.strip():
        tasks.append(asyncio.create_task(producer(_stream_anthropic(messages, anthropic_key.strip()))))

    # 3. Gemini
    if gemini_key.strip():
        tasks.append(asyncio.create_task(producer(_stream_gemini(messages, gemini_key.strip()))))

    # 4. vLLM
    if vllm_url.strip():
        tasks.append(asyncio.create_task(producer(_stream_vllm(messages, vllm_url.strip()))))

    # Fallback if no LLM provider is configured
    if not tasks:
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

    active_tasks = len(tasks)
    
    while active_tasks > 0:
        item = await queue.get()
        if item is None:
            active_tasks -= 1
        else:
            yield item

    # Add a final done event
    yield f"data: {json.dumps({'type': 'done'})}\n\n"
