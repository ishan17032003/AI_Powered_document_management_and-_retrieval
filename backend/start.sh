#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${DOCVAULT_RUNTIME_DIR:-${BACKEND_DIR}/.runtime}"
VENV_DIR="${UV_PROJECT_ENVIRONMENT:-${BACKEND_DIR}/.venv}"
DATABASE_URL_WAS_SUPPLIED=0
if [[ -v DOCVAULT_DATABASE_URL ]]; then
    DATABASE_URL_WAS_SUPPLIED=1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv is required (exact version declared in pyproject.toml)."
    echo "Install uv, then run this script again."
    exit 1
fi

cd "${BACKEND_DIR}"

# This script is the local reload entrypoint. Container deployments declare
# production explicitly and use entrypoint.sh instead.
export DOCVAULT_ENVIRONMENT="${DOCVAULT_ENVIRONMENT:-development}"
if [[ "${DOCVAULT_ENVIRONMENT}" != "development" ]]; then
    echo "Error: start.sh supports DOCVAULT_ENVIRONMENT=development only."
    echo "Use pytest for test mode or the container entrypoint for production."
    exit 64
fi

sync_args=(--frozen)
if [[ "${DOCVAULT_INSTALL_AI:-0}" == "1" ]]; then
    sync_args+=(--all-extras)
elif [[ "${DOCVAULT_INSTALL_VISUAL:-0}" == "1" ]]; then
    sync_args+=(--extra visual)
else
    # The lightweight profile is deliberately offline and model-free.
    export DOCVAULT_LLM_PROVIDER="${DOCVAULT_LLM_PROVIDER:-none}"
    export DOCVAULT_USE_DOCLING="${DOCVAULT_USE_DOCLING:-false}"
    export DOCVAULT_USE_QDRANT="${DOCVAULT_USE_QDRANT:-false}"
    export DOCVAULT_EMBEDDING_MODEL="${DOCVAULT_EMBEDDING_MODEL:-}"
    export DOCVAULT_RERANKER_MODEL="${DOCVAULT_RERANKER_MODEL:-}"
fi

# Preserve the checked-in baseline by using separate local runtime data unless
# the operator deliberately supplies other paths.
export DOCVAULT_DATABASE_URL="${DOCVAULT_DATABASE_URL:-sqlite:///${RUNTIME_DIR}/docvault.db}"
export DOCVAULT_DEBUG="${DOCVAULT_DEBUG:-true}"
export DOCVAULT_ENABLE_DEMO_SEED="${DOCVAULT_ENABLE_DEMO_SEED:-true}"
export DOCVAULT_STORAGE_DIR="${DOCVAULT_STORAGE_DIR:-${RUNTIME_DIR}/storage}"
export DOCVAULT_OKF_BUNDLE_DIR="${DOCVAULT_OKF_BUNDLE_DIR:-${RUNTIME_DIR}/okf_bundle}"
export UV_PROJECT_ENVIRONMENT="${VENV_DIR}"
export DOCVAULT_RUNTIME_DIR="${RUNTIME_DIR}"
export DOCVAULT_DATABASE_URL_WAS_SUPPLIED="${DATABASE_URL_WAS_SUPPLIED}"
mkdir -p "${DOCVAULT_STORAGE_DIR}" "${DOCVAULT_OKF_BUNDLE_DIR}"

uv sync "${sync_args[@]}"
if [[ -z "${DOCVAULT_SECRET_KEY:-}" ]]; then
    DOCVAULT_SECRET_KEY="$("${VENV_DIR}/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    export DOCVAULT_SECRET_KEY
    echo "Generated ephemeral development signing material."
fi

# Parse and validate every setting through the traceback-free launcher before
# any later helper imports application modules.
"${VENV_DIR}/bin/python" -m app.config_check

# Only the script-generated default target is eligible for automatic bootstrap,
# and only inside a non-broad DOCVAULT_RUNTIME_DIR. Caller-supplied targets and
# nonempty unversioned/unknown databases are inspected without mutation.
if "${VENV_DIR}/bin/python" -c \
    'from pathlib import Path; from app.runtime import settings; from app.schema_compatibility import is_isolated_development_sqlite; import os; raise SystemExit(0 if is_isolated_development_sqlite(settings.database_url, Path(os.environ["DOCVAULT_RUNTIME_DIR"]), database_url_was_supplied=os.environ["DOCVAULT_DATABASE_URL_WAS_SUPPLIED"] == "1") else 1)'
then
    echo "Migrating isolated development schema to the packaged head..."
    "${VENV_DIR}/bin/alembic" upgrade head
else
    if ! "${VENV_DIR}/bin/python" -m app.schema_compatibility; then
        echo "Development schema rejected (SCHEMA_MIGRATION_REQUIRED)." >&2
        echo "Run alembic upgrade head, or validate and stamp a legacy baseline before upgrading." >&2
        exit 78
    fi
    SCHEMA_ALREADY_CHECKED=1
fi
if [[ "${SCHEMA_ALREADY_CHECKED:-0}" != "1" ]]; then
    "${VENV_DIR}/bin/python" -m app.schema_compatibility
fi
"${VENV_DIR}/bin/python" -m app.seed
exec "${VENV_DIR}/bin/python" -m uvicorn \
    app.main:app \
    --reload \
    --no-access-log \
    --port 8000
