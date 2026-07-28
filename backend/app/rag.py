"""Compatibility facade for the Ask-AI/RAG service."""

from __future__ import annotations

from .services.rag_service import Answer, Passage, ask, status

__all__ = ["Answer", "Passage", "ask", "status"]
