"""Pre-download HuggingFace models into HF_HOME on startup.

Called once at ingestion-worker startup before the job loop begins.
If the model files are already present in the cache (host-mapped via the
hf_cache volume bind-mount) this is a fast no-op — huggingface_hub checks
the local snapshot before making any network request.

Models pulled:
  - BAAI/bge-m3            (embedding, dense + sparse hybrid)
  - BAAI/bge-reranker-v2-m3 (cross-encoder reranker)

Set DOCVAULT_SKIP_MODEL_PREFETCH=true to disable (e.g. fully offline env).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _skip() -> bool:
    return os.getenv("DOCVAULT_SKIP_MODEL_PREFETCH", "").lower() in {"1", "true", "yes"}


def prefetch_models(embedding_model: str, reranker_model: str) -> None:
    """Download *embedding_model* and *reranker_model* into the HF cache.

    Safe to call every startup — huggingface_hub's snapshot_download is
    idempotent and returns immediately when all blobs are already cached.
    """
    if _skip():
        log.info("model_prefetch: DOCVAULT_SKIP_MODEL_PREFETCH=true — skipping")
        return

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        log.warning(
            "model_prefetch: huggingface_hub is not installed — "
            "models will be downloaded lazily on first use instead"
        )
        return

    hf_home = os.getenv("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
    cache_path = Path(hf_home)
    try:
        cache_path.mkdir(parents=True, exist_ok=True)
        stat = cache_path.stat()
        usage = shutil.disk_usage(cache_path)
        log.info(
            "model_prefetch: HF_HOME=%s exists=%s writable=%s mode=%o "
            "owner=%s:%s free_bytes=%s uid=%s gid=%s",
            cache_path,
            cache_path.exists(),
            os.access(cache_path, os.W_OK),
            stat.st_mode & 0o777,
            stat.st_uid,
            stat.st_gid,
            usage.free,
            os.getuid(),
            os.getgid(),
        )
    except Exception:
        log.exception("model_prefetch: unable to inspect or create HF_HOME=%s", cache_path)

    for model_id in (embedding_model, reranker_model):
        if not model_id:
            log.info("model_prefetch: model setting is empty — skipping")
            continue
        started = time.monotonic()
        log.info(
            "model_prefetch: starting repo=%s cache=%s token_configured=%s",
            model_id,
            cache_path,
            bool(os.getenv("HF_TOKEN")),
        )
        try:
            local_path = snapshot_download(
                repo_id=model_id,
                # Use whatever token is configured in the environment (HF_TOKEN).
                token=os.getenv("HF_TOKEN") or None,
                # Print a progress bar to stdout so docker logs shows download progress.
                tqdm_class=None,
                # Allow the download to resume if interrupted.
                local_files_only=False,
            )
            elapsed = time.monotonic() - started
            local = Path(local_path)
            log.info(
                "model_prefetch: success repo=%s path=%s exists=%s elapsed_seconds=%.1f",
                model_id,
                local,
                local.exists(),
                elapsed,
            )
        except Exception as exc:
            # Never crash the worker on a prefetch failure — the lazy loaders
            # in search_service.py will retry (or degrade gracefully) at query time.
            log.exception(
                "model_prefetch: failed repo=%s elapsed_seconds=%.1f "
                "(%s: %s) — will attempt lazy download on first use",
                model_id,
                time.monotonic() - started,
                type(exc).__name__,
                exc,
            )
