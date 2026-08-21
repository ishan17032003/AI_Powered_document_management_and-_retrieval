# ── XENIUS DocVault — Backend ──────────────────────────────────────────────────
#
# Build context: ./backend
# Multi-stage build:
#   1. builder   – installs the exact frozen Python environment
#   2. runtime   – copies that environment + app code, installs system OCR tools
# ---------------------------------------------------------------------------

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System headers needed to compile some wheels (e.g. Pillow).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.11.9

COPY pyproject.toml uv.lock ./

# Install every production capability from the frozen lock. Development and
# test packages are deliberately excluded from the runtime image.
ENV UV_PROJECT_ENVIRONMENT="/opt/venv" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_LINK_MODE="copy"
RUN uv sync --no-dev --all-extras --no-install-project --compile-bytecode


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL maintainer="XENIUS DocVault"
LABEL description="PVVNL DDMS — FastAPI backend with multimodal RAG pipeline"

# ── System OCR & document tools ───────────────────────────────────────────────
# tesseract-ocr    : legacy OCR fallback (Tier 1)
# tesseract-ocr-eng: English language pack
# libgl1           : OpenCV / PyMuPDF runtime dep
#
# PDF parsing and rasterisation use the frozen PyMuPDF/PyPDF Python packages.
# Poppler command-line tools are intentionally absent because no application
# path invokes them; keeping them would add a second PDF stack and its network/
# cryptography dependency surface.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
        fonts-dejavu-core \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create the final runtime identity before persistent paths so a new named
# volume inherits non-root ownership from the image on first mount.
RUN groupadd --gid 10001 docvault \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --home-dir /home/docvault \
        --create-home \
        --shell /usr/sbin/nologin \
        docvault

# ── Python environment ────────────────────────────────────────────────────────
# Copy the exact virtual environment installed by the builder stage.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy application source.
COPY app/ ./app/
COPY process.sh /usr/local/bin/docvault-process
RUN chmod 0555 /usr/local/bin/docvault-process

# Ship the immutable migration history with the runtime image. Migrations are
# operator-invoked; startup compatibility enforcement is a later work package.
COPY alembic.ini ./
COPY migrations/ ./migrations/

# Copy OKF bundle seed files (copied to vlume on first boot by entrypoint.sh).

# ── Persistent data volumes ───────────────────────────────────────────────────
# These are mounted at runtime via docker-compose volumes so data survives
# container restarts/upgrades.
RUN mkdir -p \
        /data/storage \
        /data/okf_bundle \
        /hf_cache/datasets \
        /hf_cache/sentence_transformers \
        /hf_cache/torch \
        /hf_cache/xdg \
    && chown -R 10001:10001 \
        /data \
        /hf_cache \
        /home/docvault

# Entrypoint script (seed + start).
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh

# ── Runtime configuration defaults ────────────────────────────────────────────
# Signing material is deliberately absent and must be injected at runtime.
# Override other settings in docker-compose.yml or your deployment platform.
# HF_HOME / TRANSFORMERS_CACHE / SENTENCE_TRANSFORMERS_HOME all point at /hf_cache
# which is a named Docker volume — models are downloaded once and reloaded from disk
# on every subsequent restart, never re-downloaded.
ENV DOCVAULT_DATABASE_URL="sqlite:////data/docvault.db" \
    DOCVAULT_DEBUG="false" \
    DOCVAULT_ENABLE_DEMO_SEED="false" \
    DOCVAULT_ENVIRONMENT="production" \
    DOCVAULT_STORAGE_DIR="/data/storage" \
    DOCVAULT_OKF_BUNDLE_DIR="/data/okf_bundle" \
    DOCVAULT_FOLDER_IMPORT_ENABLED="false" \
    DOCVAULT_FOLDER_IMPORT_ROOTS="[]" \
    DOCVAULT_FOLDER_IMPORT_MAX_VISITED_ENTRIES="5000" \
    DOCVAULT_FOLDER_IMPORT_MAX_FILES="500" \
    DOCVAULT_FOLDER_IMPORT_MAX_TOTAL_MB="500" \
    DOCVAULT_FOLDER_IMPORT_MAX_DEPTH="10" \
    DOCVAULT_FOLDER_IMPORT_MAX_SECONDS="30" \
    DOCVAULT_LLM_PROVIDER="none" \
    DOCVAULT_ALLOW_EXTERNAL_LLM="false" \
    DOCVAULT_LLM_ALLOWED_HOSTS="[]" \
    DOCVAULT_RAG_PROVIDER_CONNECT_TIMEOUT_SECONDS="3" \
    DOCVAULT_RAG_PROVIDER_READ_TIMEOUT_SECONDS="20" \
    DOCVAULT_RAG_PROVIDER_TOTAL_TIMEOUT_SECONDS="30" \
    DOCVAULT_RAG_PROVIDER_MAX_OUTPUT_TOKENS="512" \
    DOCVAULT_RAG_MAX_CONTEXT_BYTES="32768" \
    DOCVAULT_RAG_PROVIDER_MAX_CONCURRENCY="2" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME="/home/docvault" \
    HF_HOME="/hf_cache" \
    TRANSFORMERS_CACHE="/hf_cache" \
    HF_DATASETS_CACHE="/hf_cache/datasets" \
    SENTENCE_TRANSFORMERS_HOME="/hf_cache/sentence_transformers" \
    TORCH_HOME="/hf_cache/torch" \
    XDG_CACHE_HOME="/hf_cache/xdg" \
    PATH="/opt/venv/bin:$PATH"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "app.container_healthcheck"]

# No startup step requires root. The entrypoint also refuses UID 0 so an
# accidental deployment override cannot silently weaken this boundary.
USER 10001:10001

ENTRYPOINT ["/entrypoint.sh"]
