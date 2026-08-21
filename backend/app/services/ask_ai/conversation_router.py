"""Conversation router for Ask AI.

Classifies each incoming message into one of three intents:
  - "greeting"        — short social opener, no RAG needed
  - "scope_removal"   — user wants to clear the active document/class scope
  - "rag"             — everything else, route to the RAG pipeline

Classification is pattern-matching only; no LLM call is made here so
routing adds zero latency.
"""

from __future__ import annotations

import re

from .scope_manager import is_scope_removal_request

# Single-word or very short greetings that need no document lookup
_GREETING_EXACT = frozenset({
    "hi", "hello", "hey", "yo", "sup", "howdy",
    "good morning", "good afternoon", "good evening",
    "greetings", "hiya", "what's up", "whats up",
})

_GREETING_PATTERN = re.compile(
    r"^(hi|hello|hey|yo|sup|howdy|greetings|hiya)[!.,\s]*$", re.I
)


def classify(message: str, active_scope) -> str:
    """Return 'greeting' | 'scope_removal' | 'rag'.

    Parameters
    ----------
    message:      raw user message (mentions not yet stripped)
    active_scope: current ActiveScope object (needed to decide if a removal
                  request is meaningful)
    """
    normalized = message.strip().lower()

    # Greeting check — only for very short, obviously social messages
    if normalized in _GREETING_EXACT or _GREETING_PATTERN.match(normalized):
        return "greeting"

    # Scope removal — only meaningful when there is an active scope
    if not active_scope.is_empty() and is_scope_removal_request(message):
        return "scope_removal"

    return "rag"
