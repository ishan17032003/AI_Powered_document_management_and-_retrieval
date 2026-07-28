"""Pure parsing and ranking helpers for Open Knowledge Format entries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_WORD = re.compile(r"\w+", re.UNICODE)
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class OkfEntry:
    path: Path
    title: str
    category: str = ""
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    body: str = ""
    raw: str = ""


def parse_entry(path: Path, raw: str) -> OkfEntry:
    """Parse one Markdown file while preserving the legacy fallback behavior."""
    frontmatter: dict = {}
    body = raw
    match = _FRONTMATTER.match(raw)
    if match:
        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except Exception:
            frontmatter = {}
        body = raw[match.end() :]

    title = str(frontmatter.get("title", path.stem.replace("_", " ")))
    category = str(frontmatter.get("category", ""))
    raw_tags = frontmatter.get("tags", [])
    tags = (
        [str(tag) for tag in raw_tags]
        if isinstance(raw_tags, list)
        else [str(raw_tags)]
    )
    raw_aliases = frontmatter.get("aliases", [])
    aliases = (
        [str(alias) for alias in raw_aliases] if isinstance(raw_aliases, list) else []
    )
    return OkfEntry(
        path=path,
        title=title,
        category=category,
        tags=tags,
        aliases=aliases,
        body=body.strip(),
        raw=raw,
    )


def query_tokens(question: str) -> list[str]:
    return [word.lower() for word in _WORD.findall(question) if len(word) > 2]


def entry_score(entry: OkfEntry, tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    searchable = (
        entry.title.lower()
        + " "
        + " ".join(entry.tags).lower()
        + " "
        + " ".join(entry.aliases).lower()
        + " "
        + entry.category.lower()
    )
    hits = sum(1 for token in tokens if token in searchable)
    return hits / len(tokens)
