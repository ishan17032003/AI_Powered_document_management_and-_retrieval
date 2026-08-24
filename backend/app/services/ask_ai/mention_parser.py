"""Mention parser for Ask AI.

Parses @doc:{Title} and @class:{ClassName} mentions from a user message and
returns the clean query with mentions stripped.

Examples
--------
>>> parse_mentions("@doc:{Leave Policy.pdf} What is carry-forward?")
{
    "documents": ["Leave Policy.pdf"],
    "classes": [],
    "clean_query": "What is carry-forward?"
}

>>> parse_mentions("@class:{HR Policies} What is the leave encashment rule?")
{
    "documents": [],
    "classes": ["HR Policies"],
    "clean_query": "What is the leave encashment rule?"
}
"""

from __future__ import annotations

import re


_DOC_PATTERN = re.compile(r"@doc:(?:\{([^}]+)\}|([^\s@,]+))", re.IGNORECASE)
_CLASS_PATTERN = re.compile(r"@class:(?:\{([^}]+)\}|([^\s@,]+))", re.IGNORECASE)
_DRIVE_PATTERN = re.compile(r"@drive", re.IGNORECASE)


def parse_mentions(message: str) -> dict:
    """Extract @doc, @class, and @drive mentions from a message.

    Returns
    -------
    dict with keys:
        documents: list[str]  — document titles mentioned
        classes:   list[str]  — class names mentioned
        drive:     bool       — True if @drive was mentioned
        clean_query: str      — message with all @mention tokens stripped
    """
    raw_docs = _DOC_PATTERN.findall(message)
    documents = [((m[0] or m[1]).strip()) for m in raw_docs if (m[0] or m[1]).strip()]

    raw_classes = _CLASS_PATTERN.findall(message)
    classes = [((m[0] or m[1]).strip()) for m in raw_classes if (m[0] or m[1]).strip()]

    drive = bool(_DRIVE_PATTERN.search(message))

    def _replace_doc(m: re.Match) -> str:
        title = (m.group(1) or m.group(2) or "").strip()
        return f"'{title}'" if title else ""

    def _replace_class(m: re.Match) -> str:
        name = (m.group(1) or m.group(2) or "").strip()
        return f"'{name}'" if name else ""

    clean = _DOC_PATTERN.sub(_replace_doc, message)
    clean = _CLASS_PATTERN.sub(_replace_class, clean)
    clean = _DRIVE_PATTERN.sub("Google Drive", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip()

    return {
        "documents": documents,
        "classes": classes,
        "drive": drive,
        "clean_query": clean or message.strip(),
    }



def has_any_mention(message: str) -> bool:
    """Return True if the message contains any @doc, @class, or @drive mention."""
    return bool(_DOC_PATTERN.search(message) or _CLASS_PATTERN.search(message) or _DRIVE_PATTERN.search(message))
