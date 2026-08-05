"""Eager model provisioning + warm-up for process startup.

Called from the FastAPI startup event (and the ingestion worker) so every
model the pipeline needs is downloaded (first boot) and loaded into RAM at
process start instead of on the first user request:

  - BAAI/bge-m3 + BAAI/bge-reranker-v2-m3 — prefetched into HF_HOME, then
    loaded via search_service.warm_models()
  - Docling layout/table + RapidOCR artifacts — downloaded/loaded via
    extraction_service.warm_docling()
  - SigLIP2 visual semantic model — loaded from the operator-staged,
    digest-verified artifact directory. Never downloaded here: the API must
    not fetch weights at runtime (supply-chain policy in config.py).

Everything runs on daemon threads so the /ready healthcheck and the HTTP
listener come up immediately while models provision behind it.
"""

from __future__ import annotations

import logging
import threading

from .observability import emit_event
from .utils.request_context import bound_request_context, worker_context

log = logging.getLogger(__name__)


def warm_visual_semantic() -> None:
    """Load SigLIP2 from the staged artifact and run one text forward pass.

    Spawns a daemon thread; the ~40s CPU cold-start happens at boot instead
    of on the first visual search or ingestion job. Fails loudly in the log
    (but never crashes the process) when the artifact is missing/corrupt.
    """

    context = worker_context("siglip-warm")

    def _load() -> None:
        with bound_request_context(context):
            from .runtime import settings

            if not (
                settings.visual_semantic_search_enabled
                or settings.visual_semantic_ingestion_enabled
            ):
                emit_event(
                    "worker.model_warm.completed",
                    context=context,
                    component="visual",
                    operation="siglip2",
                    outcome="disabled",
                )
                return
            emit_event(
                "worker.model_warm.started",
                context=context,
                component="visual",
                operation="siglip2",
            )
            try:
                from .services.visual_semantic_service import configured_adapter

                adapter = configured_adapter()
                adapter.embed_text(["warmup"])
                emit_event(
                    "worker.model_warm.completed",
                    context=context,
                    component="visual",
                    operation="siglip2",
                    outcome="success",
                )
            except Exception as exc:
                emit_event(
                    "worker.model_warm.completed",
                    level=logging.ERROR,
                    context=context,
                    component="visual",
                    operation="siglip2",
                    outcome="error",
                    error=exc,
                )

    threading.Thread(target=_load, name="siglip-warm", daemon=True).start()


def warm_all_models() -> None:
    """Provision and load every pipeline model in the background.

    Ordering: the HF prefetch (network download when absent) completes before
    warm_models() loads those weights; Docling and SigLIP2 then warm on their
    own threads concurrently.
    """

    def _run() -> None:
        from .runtime import settings

        try:
            from .model_prefetch import prefetch_models

            prefetch_models(
                embedding_model=settings.embedding_model,
                reranker_model=settings.reranker_model,
            )
        except Exception:
            log.exception("model_warmup: embedding/reranker prefetch failed")

        try:
            from .services.search_service import warm_models

            warm_models()
        except Exception:
            log.exception("model_warmup: retrieval model warm-up failed")

        try:
            from .services.extraction_service import warm_docling

            warm_docling()
        except Exception:
            log.exception("model_warmup: docling warm-up failed")

        warm_visual_semantic()

    threading.Thread(target=_run, name="model-warmup", daemon=True).start()
