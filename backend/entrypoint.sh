#!/bin/bash
# entrypoint.sh — validate a pre-migrated DB, then start the API server.
# Runs inside the backend container as PID 1.
#
# Can also be sourced locally for testing — env vars default to local paths.
set -e
umask 027

# ── Defaults (used when running outside Docker, e.g. local testing) ───────────
export DOCVAULT_DATABASE_URL="${DOCVAULT_DATABASE_URL:-sqlite:///./docvault.db}"
export DOCVAULT_ENVIRONMENT="${DOCVAULT_ENVIRONMENT:-production}"
export DOCVAULT_STORAGE_DIR="${DOCVAULT_STORAGE_DIR:-./storage}"
export DOCVAULT_OKF_BUNDLE_DIR="${DOCVAULT_OKF_BUNDLE_DIR:-./okf_bundle}"
export DOCVAULT_SECRET_KEY_FILE="${DOCVAULT_SECRET_KEY_FILE:-/run/secrets/docvault_secret_key}"

# Production secrets are mounted by the orchestrator, never baked into the
# image or committed to Compose. Keep the file-to-environment bridge here so
# the existing validated settings contract remains unchanged.
if [ -n "${DOCVAULT_SECRET_KEY_FILE:-}" ] && [ -r "${DOCVAULT_SECRET_KEY_FILE}" ]; then
    DOCVAULT_SECRET_KEY="$(cat -- "${DOCVAULT_SECRET_KEY_FILE}")"
    export DOCVAULT_SECRET_KEY
fi

if [ "$(id -u)" -eq 0 ]; then
    echo "[entrypoint] Refusing to run the backend as UID 0." >&2
    exit 77
fi

case "${DOCVAULT_ENVIRONMENT}" in
    development|production)
        ;;
    test)
        echo "[entrypoint] Test mode does not launch a persistent API server." >&2
        exit 64
        ;;
    *)
        echo "[entrypoint] Invalid DOCVAULT_ENVIRONMENT; expected development or production." >&2
        exit 64
        ;;
esac

# Development gets process-local signing material without committing or logging
# a reusable secret. Production must inject DOCVAULT_SECRET_KEY and fails closed
# below when it is absent or unsafe.
if [ "${DOCVAULT_ENVIRONMENT}" = "development" ] && [ -z "${DOCVAULT_SECRET_KEY:-}" ]; then
    DOCVAULT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    export DOCVAULT_SECRET_KEY
    echo "[entrypoint] Generated ephemeral development signing material."
fi

# Validate the complete production contract before logging configuration,
# creating directories, touching the database, seeding, loading models, or
# starting a process that can make outbound calls.
python -m app.config_check

echo "═══════════════════════════════════════════════════"
echo " XENIUS DocVault — backend starting"
echo "═══════════════════════════════════════════════════"
echo "  ENVIRONMENT: ${DOCVAULT_ENVIRONMENT}"
echo "  CONFIG   : validated (values redacted)"
echo "═══════════════════════════════════════════════════"

# Ensure every declared runtime path is usable by the final identity. This gives
# operators a deterministic failure for an old/root-owned volume instead of a
# later partial database, upload, or model-cache failure.
ensure_writable_directory() {
    runtime_path="$1"
    runtime_label="$2"

    if ! mkdir -p -- "${runtime_path}"; then
        echo "[entrypoint] ${runtime_label} cannot be created by the runtime identity." >&2
        exit 73
    fi
    if [ ! -d "${runtime_path}" ] || [ ! -w "${runtime_path}" ] || [ ! -x "${runtime_path}" ]; then
        echo "[entrypoint] ${runtime_label} is not writable by the runtime identity." >&2
        exit 73
    fi
}

ensure_writable_directory "${DOCVAULT_STORAGE_DIR}" "Storage directory"

# OKF bundle is optional; if it's a root-owned bind mount, don't fail the startup.
if ! mkdir -p -- "${DOCVAULT_OKF_BUNDLE_DIR}" 2>/dev/null; then
    echo "[entrypoint] WARNING: OKF bundle directory cannot be created (likely a root-owned bind mount). OKF features may be degraded." >&2
elif [ ! -w "${DOCVAULT_OKF_BUNDLE_DIR}" ]; then
    echo "[entrypoint] WARNING: OKF bundle directory is not writable." >&2
fi

case "${DOCVAULT_DATABASE_URL}" in
    sqlite:///*)
        SQLITE_DATABASE_PATH="${DOCVAULT_DATABASE_URL#sqlite:///}"
        SQLITE_DATABASE_PATH="${SQLITE_DATABASE_PATH%%\?*}"
        ensure_writable_directory "$(dirname -- "${SQLITE_DATABASE_PATH}")" "SQLite directory"
        ;;
esac

for cache_path in \
    "${HF_HOME:-}" \
    "${TRANSFORMERS_CACHE:-}" \
    "${HF_DATASETS_CACHE:-}" \
    "${SENTENCE_TRANSFORMERS_HOME:-}" \
    "${TORCH_HOME:-}" \
    "${XDG_CACHE_HOME:-}"
do
    if [ -n "${cache_path}" ]; then
        ensure_writable_directory "${cache_path}" "Model cache directory"
    fi
done

# Compatibility is a read-only gate. It never creates, stamps, upgrades,
# downgrades, or repairs a schema. The deployment migration job must reach the
# packaged head before this process may seed or launch a web worker.
python -m app.schema_compatibility

# Demo seed is explicit and development/test-only. app.seed safely skips when
# DOCVAULT_ENABLE_DEMO_SEED is false and production validation rejects true.
echo "[entrypoint] Checking explicit demo seed..."
python -m app.seed && echo "[entrypoint] Seed OK."

# Copy OKF bundle sample files into the mounted volume only if the
# bundle dir is empty (i.e. first boot with a fresh volume).
OKF_SOURCE="/app/okf_bundle"
if [ -d "${OKF_SOURCE}" ] && [ "$(ls -A ${OKF_SOURCE} 2>/dev/null)" ]; then
    if [ -z "$(ls -A ${DOCVAULT_OKF_BUNDLE_DIR} 2>/dev/null)" ]; then
        echo "[entrypoint] Populating OKF bundle with default entries..."
        cp "${OKF_SOURCE}"/* "${DOCVAULT_OKF_BUNDLE_DIR}/" 2>/dev/null || true
    fi
fi

# Heavy embedding, reranker, and OCR models remain lazy. Web startup and
# readiness never download or initialize them; model prefetch belongs in a
# separate deployment job with its own timeout and retry policy.

# ── Start the application ─────────────────────────────────────────────────────
# Production:  gunicorn with multiple uvicorn workers.
# Development: uvicorn with reload. The legacy DOCVAULT_DEV switch is not used.
WORKERS="${GUNICORN_WORKERS:-2}"

if [ "${DOCVAULT_ENVIRONMENT}" = "development" ]; then
    echo "[entrypoint] DEV mode — starting uvicorn with --reload ..."
    exec uvicorn \
        app.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --reload \
        --no-access-log
else
    echo "[entrypoint] Starting Gunicorn with ${WORKERS} uvicorn worker(s)..."
    exec gunicorn app.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "${WORKERS}" \
        --bind 0.0.0.0:8000 \
        --timeout 300 \
        --graceful-timeout 30 \
        --error-logfile - \
        --log-level info
fi
