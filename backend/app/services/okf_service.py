"""Open Knowledge Format (OKF) — fast-path retrieval for stable organisational knowledge.

What is OKF?
  A Git-versioned Markdown knowledge bundle where each concept lives in its own
  .md file with YAML frontmatter (keys: title, category, tags, aliases).

  Example file layout:
    okf_bundle/
      metric_at_losses.md
      policy_outage_response.md
      schema_oms_feeder.md
      glossary_load_factor.md

Why?
  - RAG is best for large, ever-changing document collections.
  - OKF is best for stable definitions (metrics, schemas, runbooks, policies,
    glossaries).  Routing stable-knowledge queries here avoids re-retrieval and
    gives governed, single-source-of-truth answers.

Fast-path routing:
  OKF answers a query when query terms overlap strongly with a bundle entry's
  title / tags / aliases.  If a match is found, the relevant OKF content is
  prepended to the passage list so the LLM cites it as source [OKF].

Bundle directory is configured via DOCVAULT_OKF_BUNDLE_DIR (default: backend/okf_bundle/).
The bundle is loaded into memory on first use and is refreshed when the server restarts.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock

from ..config import settings
from ..repositories import okf_repository
from ..utils.okf_helpers import OkfEntry, entry_score, query_tokens

# ── In-memory bundle ──────────────────────────────────────────────────────────

_bundle: list[OkfEntry] | None = None
_bundle_revision: okf_repository.BundleRevision | None = None
_bundle_lock = RLock()
_MAX_STABLE_LOAD_ATTEMPTS = 3


def _load_bundle() -> list[OkfEntry]:
    return okf_repository.load_bundle(settings.okf_bundle_dir)


def _load_stable_bundle() -> tuple[
    list[OkfEntry],
    okf_repository.BundleRevision | None,
]:
    """Load only a revision that stayed unchanged for the complete read.

    A bounded failure returns an empty bundle with no cached revision. This
    fails closed for the current request and forces the next request to retry
    instead of pinning stale or mixed knowledge to a newer revision.
    """

    for _ in range(_MAX_STABLE_LOAD_ATTEMPTS):
        before = okf_repository.bundle_revision(settings.okf_bundle_dir)
        loaded = _load_bundle()
        after = okf_repository.bundle_revision(settings.okf_bundle_dir)
        if before == after:
            return loaded, after
    return [], None


def get_bundle() -> list[OkfEntry]:
    global _bundle, _bundle_revision
    with _bundle_lock:
        current_revision = okf_repository.bundle_revision(settings.okf_bundle_dir)
        if _bundle is None or current_revision != _bundle_revision:
            _bundle, _bundle_revision = _load_stable_bundle()
        return _bundle


def reload_bundle() -> int:
    """Reload this worker; other workers detect the filesystem revision."""

    global _bundle, _bundle_revision
    with _bundle_lock:
        _bundle, _bundle_revision = _load_stable_bundle()
        return len(_bundle)


# ── Query matching ────────────────────────────────────────────────────────────


def _entry_score(entry: OkfEntry, query_tokens: list[str]) -> float:
    return entry_score(entry, query_tokens)


def query_okf(question: str, top_k: int = 2, threshold: float = 0.4) -> list[OkfEntry]:
    """Return OKF entries that strongly match the question.

    Returns an empty list when:
    - The bundle is empty or the directory does not exist.
    - No entry reaches the threshold score.
    """
    bundle = get_bundle()
    if not bundle:
        return []

    tokens = query_tokens(question)
    if not tokens:
        return []

    scored = [(e, _entry_score(e, tokens)) for e in bundle]
    scored = [(e, s) for e, s in scored if s >= threshold]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [e for e, _ in scored[:top_k]]


# ── Bundle management helpers ──────────────────────────────────────────────────


def bundle_status() -> dict:
    """Return a status dict for the /api/v1/status endpoint."""
    bundle_dir: Path = settings.okf_bundle_dir
    entries = get_bundle()
    return {
        "enabled": bundle_dir.exists(),
        "bundle_dir": str(bundle_dir),
        "entry_count": len(entries),
        "categories": list({e.category for e in entries if e.category}),
    }


def save_entry(filename: str, content: str) -> OkfEntry:
    """Write (or overwrite) a Markdown file in the OKF bundle and reload."""
    dest = okf_repository.save_entry(settings.okf_bundle_dir, filename, content)
    reload_bundle()
    # Return the freshly loaded entry.
    for e in get_bundle():
        if e.path == dest:
            return e
    # Fallback: construct a minimal entry.
    return OkfEntry(path=dest, title=filename, body=content, raw=content)


def list_entry_summaries() -> list[dict]:
    return [
        {
            "filename": entry.path.name,
            "title": entry.title,
            "category": entry.category,
            "tags": entry.tags,
            "aliases": entry.aliases,
            "body_preview": entry.body[:200] + ("…" if len(entry.body) > 200 else ""),
        }
        for entry in get_bundle()
    ]


def create_entry(filename: str, content: str) -> dict:
    """Normalize the HTTP create request and return its established response shape."""
    normalized_filename = filename if filename.endswith(".md") else filename + ".md"
    entry = save_entry(normalized_filename, content)
    return {
        "status": "saved",
        "filename": normalized_filename,
        "title": entry.title,
        "bundle_size": len(get_bundle()),
    }
