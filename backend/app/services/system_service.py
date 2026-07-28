"""Side-effect-free system probes and authenticated diagnostics."""

from __future__ import annotations

import os

from .. import __version__
from ..runtime import settings
from ..schema_compatibility import assert_schema_compatible


def live_payload() -> dict:
    """Return process liveness without DB, filesystem, model, or network access."""

    return {
        "status": "live",
        "app": settings.app_name,
        "version": __version__,
    }


def legacy_health_payload() -> dict:
    """Preserve the old health response shape using passive capability state."""

    from . import extraction_service

    return {
        "status": "ok",
        "app": settings.app_name,
        "version": __version__,
        "ocr_engines": extraction_service.passive_engine_status(),
    }


def readiness_checks() -> dict[str, bool]:
    """Check required dependencies without model initialization or egress.

    Schema compatibility is intentionally re-inspected rather than cached so a
    post-start head-stamp or critical-structure drift fails readiness promptly.
    The probe is read-only and bounded to service-critical objects.
    """

    try:
        assert_schema_compatible(settings.database_url)
        database_ready = True
    except Exception:
        database_ready = False

    def usable_directory(path) -> bool:
        return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)

    return {
        "database": database_ready,
        "storage": usable_directory(settings.storage_dir),
        "okf_bundle": usable_directory(settings.okf_bundle_dir),
    }


def ready_payload() -> tuple[dict, bool]:
    checks = readiness_checks()
    ready = all(checks.values())
    return {"status": "ready" if ready else "not_ready"}, ready


def status_payload() -> dict:
    """Return detailed diagnostics only after HTTP authentication succeeds."""

    from . import extraction_service, rag_service

    rag = rag_service.status()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": __version__,
        "dependencies": readiness_checks(),
        "rag": rag,
        "models_ready": {
            "embedding": rag["search"]["embedding_model"] is not None,
            "reranker": rag["search"]["reranker_model"] is not None,
            "qdrant": rag["search"]["qdrant"],
        },
        "ocr_engines": extraction_service.engine_status(),
    }
