"""Answer synthesis layer for Ask AI.

Combines Company KB and Google Drive results into one final grounded answer.
Uses the configured vLLM / external LLM provider, with graceful extractive
fallback if the LLM provider is offline.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ...config import settings

_log = logging.getLogger(__name__)


class SynthesisResult:
    """Lightweight container for the combined answer."""

    __slots__ = ("answer", "mode", "citations", "drive_sources")

    def __init__(
        self,
        answer: str,
        mode: str,
        citations: list[dict],
        drive_sources: list[dict] | None = None,
    ) -> None:
        self.answer = answer
        self.mode = mode
        self.citations = citations
        self.drive_sources = drive_sources or []


async def synthesize(
    question: str,
    history: list[dict],
    kb_result,  # rag_service.Answer | None
    drive_files: list[dict] | None,
) -> SynthesisResult:
    """Synthesize a final answer from KB and/or Drive results."""
    has_kb = kb_result is not None and kb_result.mode not in ("notfound", "insufficient_evidence")
    has_drive = bool(drive_files)

    # ── KB only ────────────────────────────────────────────────────────────────
    if has_kb and not has_drive:
        return SynthesisResult(
            answer=kb_result.answer,
            mode=kb_result.mode,
            citations=[dict(c) if hasattr(c, "__iter__") else c for c in (kb_result.citations or [])],
        )

    # ── Drive only ─────────────────────────────────────────────────────────────
    if has_drive and not has_kb:
        answer = await _synthesize_drive_only(question, drive_files, history)
        drive_sources = _drive_source_summaries(drive_files)
        return SynthesisResult(
            answer=answer,
            mode="gdrive",
            citations=[],
            drive_sources=drive_sources,
        )

    # ── Both ───────────────────────────────────────────────────────────────────
    if has_kb and has_drive:
        answer = await _synthesize_combined(question, kb_result, drive_files, history)
        drive_sources = _drive_source_summaries(drive_files)
        citations = [dict(c) if hasattr(c, "__iter__") else c for c in (kb_result.citations or [])]
        return SynthesisResult(
            answer=answer,
            mode="combined",
            citations=citations,
            drive_sources=drive_sources,
        )

    # ── Neither ────────────────────────────────────────────────────────────────
    fallback = (
        kb_result.answer
        if kb_result is not None
        else "I couldn't find relevant information from the selected sources."
    )
    return SynthesisResult(
        answer=fallback,
        mode=kb_result.mode if kb_result is not None else "notfound",
        citations=[],
    )


# ── LLM Invocation Helper ────────────────────────────────────────────────────


async def _call_llm(messages: list[dict[str, str]], max_tokens: int = 1024) -> str | None:
    """Call vLLM or OpenAI-compatible endpoint with messages."""
    vllm_url = (settings.vllm_url or "").rstrip("/")
    if not vllm_url:
        return None

    url = f"{vllm_url}/chat/completions"
    model = settings.vllm_model or "gemma-4-31b"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            res = await client.post(url, json=payload)
            if res.status_code == 200:
                data = res.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
            else:
                _log.warning("vLLM call returned status %s: %s", res.status_code, res.text)
    except Exception as exc:
        _log.warning("vLLM call failed (%s): %s", url, exc)

    return None


# ── Private synthesis helpers ─────────────────────────────────────────────────


async def _synthesize_drive_only(
    question: str,
    files: list[dict],
    history: list[dict] | None = None,
) -> str:
    """Generate an answer using Drive files only."""
    # Build Drive document contexts
    context_blocks = []
    for i, f in enumerate(files, 1):
        name = f.get("name", f"Document {i}")
        content = (f.get("content") or "").strip()
        if content:
            context_blocks.append(f"### [Drive File {i}: {name}]\n{content[:3500]}")

    context_str = "\n\n".join(context_blocks)

    system_prompt = (
        "You are an AI assistant helping a user find information from their Google Drive files. "
        "Answer the user's question accurately and thoroughly based ONLY on the provided Google Drive context. "
        "Cite the document name like [Drive: filename] for specific facts. "
        "If the information is not present in the files, state clearly what could not be found."
    )

    user_prompt = f"Question: {question}\n\nGoogle Drive Context:\n{context_str}\n\nAnswer:"

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        for turn in history[-4:]:
            messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
    messages.append({"role": "user", "content": user_prompt})

    answer = await _call_llm(messages)
    if answer:
        return answer

    return _fallback_drive_answer(question, files)


async def _synthesize_combined(
    question: str,
    kb_result,
    drive_files: list[dict],
    history: list[dict] | None = None,
) -> str:
    """Combine KB passages and Drive content into one unified answer."""
    kb_section = f"Company Knowledge Base:\n{kb_result.answer[:3000]}"

    drive_blocks = []
    for i, f in enumerate(drive_files, 1):
        name = f.get("name", f"Document {i}")
        content = (f.get("content") or "").strip()
        if content:
            drive_blocks.append(f"### [Drive File {i}: {name}]\n{content[:2500]}")
    drive_section = "Google Drive Files:\n" + "\n\n".join(drive_blocks)

    system_prompt = (
        "You are an enterprise AI assistant synthesizing information from two sources: "
        "the Company Knowledge Base and the user's personal Google Drive. "
        "Produce a single, well-structured answer combining relevant points from both sources. "
        "Cite [KB] when referencing company knowledge, and [Drive: filename] when referencing Google Drive files."
    )

    user_prompt = (
        f"Question: {question}\n\n"
        f"--- Source 1: Company Knowledge Base ---\n{kb_section}\n\n"
        f"--- Source 2: Google Drive ---\n{drive_section}\n\n"
        f"Unified Answer:"
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        for turn in history[-4:]:
            messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
    messages.append({"role": "user", "content": user_prompt})

    answer = await _call_llm(messages)
    if answer:
        return answer

    # Fallback to combined text
    return f"{kb_result.answer}\n\n---\n\n" + _fallback_drive_answer(question, drive_files)


def _fallback_drive_answer(question: str, files: list[dict]) -> str:
    """Extractive fallback when LLM is unavailable for Drive synthesis."""
    lines = [f"Found {len(files)} relevant Google Drive document(s):"]
    for i, f in enumerate(files, 1):
        name = f.get("name", f"File {i}")
        snippet = (f.get("content") or "")[:400].replace("\n", " ").strip()
        lines.append(f"\n📄 **{name}**\n> {snippet}…\n")
    return "\n".join(lines)


def _drive_source_summaries(files: list[dict]) -> list[dict]:
    """Build the drive_sources list for the API response."""
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mimeType": f.get("mimeType"),
            "webViewLink": f.get("webViewLink", ""),
            "matched_keywords": f.get("matched_keywords", []),
            "snippet": (f.get("content") or "")[:280],
        }
        for f in files
    ]
