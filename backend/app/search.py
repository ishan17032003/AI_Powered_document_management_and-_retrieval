"""Compatibility facade for hybrid search.

The implementation lives in :mod:`app.services.search_service`.  Keep this
module stable because document ingestion, duplicate handling, application
startup, and external callers import these functions from ``app.search``.
"""

from __future__ import annotations

from .services.search_service import (
    index_document,
    remove_document,
    search,
    search_status,
    warm_models,
)

__all__ = [
    "index_document",
    "remove_document",
    "search",
    "search_status",
    "warm_models",
]
