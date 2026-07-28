"""Pure text helpers shared by search and RAG."""

from __future__ import annotations

import re

_WORD = re.compile(r"\w+", re.UNICODE)

_SEARCH_STOPWORDS = {
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
    "been",
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
    "say",
    "says",
    "said",
    "there",
    "here",
    "any",
    "all",
    "some",
    "into",
    "so",
    "if",
    "then",
    "summary",
    "summarize",
    "summery",
}


def stem(word: str) -> str:
    """Apply the small legacy stemmer used by both retrieval paths."""
    value = word.lower()
    if len(value) > 4:
        if value.endswith("ies"):
            return value[:-3] + "y"
        if (
            value.endswith("es")
            and not value.endswith("aes")
            and not value.endswith("ees")
            and not value.endswith("oes")
        ):
            return value[:-2]
        if (
            value.endswith("s")
            and not value.endswith("ss")
            and not value.endswith("us")
            and not value.endswith("is")
        ):
            return value[:-1]
        if value.endswith("ing"):
            return value[:-3]
        if value.endswith("ed"):
            return value[:-2]
    return value


def to_match_query(query: str) -> str:
    """Turn user text into the legacy FTS5 OR/prefix expression."""
    words = [word.lower() for word in _WORD.findall(query)]
    terms = [word for word in words if word not in _SEARCH_STOPWORDS] or words
    if not terms:
        return ""

    stemmed_terms: list[str] = []
    for term in terms:
        normalized = stem(term)
        stemmed_terms.append(normalized)
        if normalized != term:
            stemmed_terms.append(term)
    return " OR ".join(f"{term}*" for term in stemmed_terms)
