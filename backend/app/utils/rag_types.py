"""Internal return types retained by the public RAG compatibility facade."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Passage:
    index: int
    document_id: int
    title: str
    text: str
    source: str = "rag"
    # Retrieval lineage used by the auditable context manifest.  Legacy
    # document passages may leave these unset.
    chunk_id: str | None = None
    version_id: str | int | None = None
    page: int | None = None
    evidence_type: str = "text"
    asset_id: str | int | None = None


@dataclass
class Answer:
    answer: str
    mode: str
    citations: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    needs_clarification: bool = False
    scoped_document_id: int | None = None
    model: str | None = None
