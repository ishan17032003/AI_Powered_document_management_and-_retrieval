"""Pure query-understanding and passage-window helpers for RAG."""

from __future__ import annotations

import re

from .search_helpers import stem

_WORD = re.compile(r"\w+", re.UNICODE)
_SUMMARY_RE = re.compile(r"summ(a|e)r", re.IGNORECASE)

_QUERY_STOP = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "is",
    "are",
    "was",
    "were",
    "be",
    "what",
    "who",
    "how",
    "why",
    "when",
    "where",
    "which",
    "in",
    "on",
    "for",
    "and",
    "or",
    "do",
    "does",
    "did",
    "with",
    "this",
    "that",
    "these",
    "those",
    "from",
    "by",
    "at",
    "as",
    "it",
    "its",
    "i",
    "you",
    "your",
    "me",
    "my",
    "we",
    "our",
    "they",
    "he",
    "she",
    "can",
    "could",
    "would",
    "should",
    "will",
    "shall",
    "may",
    "might",
    "give",
    "tell",
    "show",
    "get",
    "find",
    "want",
    "need",
    "please",
    "about",
    "summary",
    "summarize",
    "summarise",
    "summery",
    "explain",
    "describe",
    "details",
    "detail",
    "say",
    "says",
    "said",
    "there",
    "here",
    "any",
    "all",
    "some",
    "into",
    "out",
    "up",
    "down",
    "so",
    "if",
    "then",
    "document",
    "doc",
    "file",
    "files",
    "content",
    "contents",
    "no",
    "yes",
    "ok",
    "okay",
    "bullet",
    "bullets",
    "point",
    "points",
    "list",
    "lists",
    "brief",
    "briefly",
    "crux",
    "gist",
    "overview",
    "key",
    "main",
    "important",
    "info",
    "information",
    "regarding",
    "concerning",
}

_ACRONYMS = {
    "brd": ["business requirements document", "business requirement document"],
    "frd": ["functional requirements document", "functional requirement document"],
    "wfms": ["work force management system", "workforce management system"],
    "p2p": ["peer to peer", "peer-to-peer"],
    "evcd": ["electric vehicle charging device"],
}


def query_terms(question: str) -> list[str]:
    return [
        word.lower()
        for word in _WORD.findall(question)
        if word.lower() not in _QUERY_STOP and len(word) > 1
    ]


def title_score(title: str, terms: list[str]) -> int:
    lowered = title.lower()
    score = 0
    for term in terms:
        if term in lowered:
            score += 1
            continue
        for expansion in _ACRONYMS.get(term, []):
            if expansion in lowered:
                score += 1
                break
    return score


def relevant_window(text: str, terms: list[str], size: int) -> str:
    if not terms or not text:
        return text[:size]
    lowered = text.lower()
    stems = {stem(term) for term in terms}
    positions: list[int] = []
    for value in stems:
        start = 0
        while True:
            position = lowered.find(value, start)
            if position < 0:
                break
            positions.append(position)
            start = position + len(value)
    if not positions:
        return text[:size]

    positions.sort()
    best_start, best_count = positions[0], 0
    for position in positions:
        count = sum(
            1 for candidate in positions if position <= candidate < position + size
        )
        if count > best_count:
            best_start, best_count = position, count
    begin = max(0, best_start - size // 4)
    snippet = text[begin : begin + size]
    prefix = "…" if begin > 0 else ""
    suffix = "…" if begin + size < len(text) else ""
    return f"{prefix}{snippet.strip()}{suffix}"


def is_summary(question: str) -> bool:
    return bool(_SUMMARY_RE.search(question))
