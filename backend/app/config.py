"""Application settings. Env-overridable with the DOCVAULT_ prefix."""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # pydantic-settings 2.2 no longer re-exports this legacy exception.
    from pydantic_settings import SettingsError
except ImportError:  # pragma: no cover - version compatibility shim
    SettingsError = ValueError  # type: ignore[misc,assignment]

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


class RuntimeEnvironment(StrEnum):
    """Supported runtime profiles; aliases and implicit production are forbidden."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class RetrievalReadMode(StrEnum):
    """Explicit, immediately reversible retrieval-serving modes."""

    CURRENT = "current"
    LANCEDB_SHADOW = "lancedb_shadow"
    LANCEDB_PRIMARY = "lancedb_primary"


# Docker secrets mount (compose `secrets:` block). File names map to settings
# fields with the env prefix, e.g. /run/secrets/docvault_secret_key ->
# secret_key. Absent outside containers, where env/.env remain the sources.
_SECRETS_DIR = "/run/secrets" if Path("/run/secrets").is_dir() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCVAULT_",
        # Single shared env file at the repository root (also read by Docker
        # Compose); a backend-local .env still wins if one exists.
        env_file=(str(BASE_DIR.parent / ".env"), ".env", "/data/.env"),
        secrets_dir=_SECRETS_DIR,
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: RuntimeEnvironment = RuntimeEnvironment.DEVELOPMENT
    app_name: str = "XENIUS DocVault"
    debug: bool = False
    enable_demo_seed: bool = False
    # Convenience only for a local demo; production validation rejects this.
    demo_fixed_credentials: bool = False
    # No signing material is shipped in source. Production validation requires
    # an injected, high-entropy value; local entrypoints generate an ephemeral
    # value when the operator has not supplied one.
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_minutes: int = Field(default=15, ge=1, le=1440)
    jwt_issuer: str = "docvault"
    jwt_audience: str = "docvault-api"
    # D-06 decision: hardened local authentication for the first controlled
    # release. OIDC/SSO is not silently enabled without an approved provider.
    identity_provider: Literal["local"] = "local"
    mfa_required: bool = False

    # Login abuse controls.  These are intentionally process-local admission
    # bounds; a shared/durable limiter and coordinated lockout policy remain a
    # separate production work item.
    login_rate_limit_enabled: bool = True
    login_account_failure_limit: int = Field(default=5, ge=1, le=1000)
    login_source_failure_limit: int = Field(default=20, ge=1, le=5000)
    login_failure_window_seconds: int = Field(default=300, ge=1, le=86_400)
    login_block_seconds: int = Field(default=60, ge=1, le=86_400)
    login_rate_limit_max_entries: int = Field(default=10_000, ge=2, le=100_000)

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/docvault"
    storage_dir: Path = BASE_DIR / "storage"

    # ── Object storage backend ───────────────────────────────────────────────
    # Set to "minio" to route all document binaries through MinIO.
    # "filesystem" retains the original local-path implementation.
    storage_backend: Literal["filesystem", "minio"] = "filesystem"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    # All per-class buckets share this prefix: <prefix>-<classname>
    minio_bucket_prefix: str = "docvault"

    # Client address forwarding is disabled by default.  When an operator
    # explicitly lists the network(s) occupied by the ingress proxy, request
    # context may consume a bounded X-Forwarded-For chain from that proxy.
    # Values are canonicalized here so malformed configuration fails at
    # startup without ever being copied into an error response or log.
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)

    max_upload_mb: int = Field(default=50, ge=1, le=2048)
    max_archive_entries: int = Field(default=10_000, ge=1, le=1_000_000)
    max_archive_uncompressed_mb: int = Field(default=500, ge=1, le=10_000)
    max_archive_compression_ratio: int = Field(default=1_000, ge=1, le=100_000)
    max_document_pages: int = Field(default=10_000, ge=1, le=1_000_000)
    max_image_pixels: int = Field(default=100_000_000, ge=1, le=2_000_000_000)
    max_extracted_text_chars: int = Field(default=5_000_000, ge=1, le=100_000_000)
    max_extraction_seconds: int = Field(default=120, ge=1, le=3600)
    # OCR runs with the intersection of these requested languages and the
    # language packs installed in the worker image.  The selected and missing
    # packs are persisted as extraction provenance for each document version.
    ocr_languages: list[str] = Field(default_factory=lambda: ["eng", "hin"])
    extraction_review_quality_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
    )
    ingestion_worker_count: int = Field(default=1, ge=1, le=32)
    enable_embeddings: bool = False
    retrieval_read_mode: RetrievalReadMode = RetrievalReadMode.CURRENT
    lancedb_uri: Path = BASE_DIR / "lancedb"
    lancedb_table_name: str = Field(
        default="text_chunks",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$",
    )
    # Only the dedicated ingestion/maintenance process may enable mutations.
    # API readers always construct a read-only adapter even if this setting is
    # accidentally enabled in their environment.
    lancedb_writer_enabled: bool = False
    lancedb_lock_timeout_seconds: float = Field(default=5.0, ge=0.05, le=300.0)
    # Store BGE-M3 dense vectors on text_chunks rows and serve text retrieval
    # as a BM25+vector hybrid with cross-encoder reranking — no Qdrant involved.
    lancedb_text_vectors_enabled: bool = False

    # Text-to-visual retrieval uses the authoritative visual asset/extraction
    # records and exact SQL authorization.  The lexical lane is deliberately
    # available without downloading a model; semantic CLIP-style retrieval is
    # a separate, pinned model/index promotion.
    visual_search_enabled: bool = True
    visual_search_max_candidates: int = Field(default=500, ge=20, le=10_000)
    visual_max_assets_per_version: int = Field(default=2_000, ge=1, le=100_000)

    # Semantic image/text retrieval is opt-in independently from the safe
    # lexical visual lane. When enabled, the startup warm-up downloads the
    # pinned model into persistent storage; request handling remains local-only.
    visual_semantic_search_enabled: bool = True
    visual_semantic_ingestion_enabled: bool = True
    visual_semantic_model_path: Path | None = None
    visual_semantic_model_revision: str = "google/siglip2-base-patch16-224"
    visual_semantic_model_sha256: str = ""
    visual_semantic_dimension: int = Field(default=768, ge=8, le=4096)
    visual_semantic_device: Literal["auto", "cpu", "cuda"] = "auto"
    visual_semantic_max_batch: int = Field(default=4, ge=1, le=64)
    visual_semantic_max_query_assets: int = Field(default=5000, ge=20, le=100_000)
    visual_semantic_prefilter_max_ids: int = Field(default=10_000, ge=20, le=100_000)
    # A semantic index always has a nearest neighbour.  Keep weak matches out
    # of the public result set by default; operators can calibrate this floor
    # against their approved visual evaluation corpus.
    visual_semantic_min_score: float = Field(default=0.08, ge=0.0, le=1.0)
    visual_semantic_query_timeout_seconds: float = Field(default=20.0, ge=1.0, le=300.0)
    visual_semantic_manifest_version: str = "visual-semantic-v1"

    # Server-side folder import is an exceptional operator capability. It is
    # disabled until explicit roots and bounded enumeration limits are supplied.
    folder_import_enabled: bool = False
    folder_import_roots: list[Path] = []
    folder_import_max_visited_entries: int = 5000
    folder_import_max_files: int = 500
    folder_import_max_total_mb: int = 500
    folder_import_max_depth: int = 10
    folder_import_max_seconds: int = 30

    # RAG / Ask-AI answer generation. The safe default never makes an LLM
    # request. Any networked provider additionally requires an explicit egress
    # opt-in and exact destination-host allowlist.
    #   "vllm"      -> local vLLM (OpenAI-compatible API) only
    #   "ollama"    -> local open-weight model only (offline, India-resident)
    #   "anthropic" -> Claude only
    #   "none"      -> extractive passages only
    llm_provider: str = "vllm"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    
    allow_external_llm: bool = False
    # Development-only escape hatch: permit plain-HTTP egress to an allowlisted
    # external provider host. Keep False anywhere document content matters.
    allow_insecure_llm_http: bool = False
    llm_allowed_hosts: list[str] = []
    # Context window for answer generation. Increased to allow larger/more chunks for better understanding.
    rag_max_context_chars: int = 35000
    # Provider work has a per-process, no-queue admission cap. The web request
    # stops waiting at the total deadline even if a provider library has not
    # returned; provider threads remain capped and phase-timeout protected.
    rag_provider_connect_timeout_seconds: float = 3.0
    rag_provider_read_timeout_seconds: float = 20.0
    rag_provider_total_timeout_seconds: float = 30.0
    rag_provider_max_output_tokens: int = 512
    rag_max_context_bytes: int = 32_768
    rag_provider_max_concurrency: int = 2

    # local vLLM (OpenAI-compatible API)
    vllm_url: str = ""
    vllm_model: str = "gemma-4-31b"

    # Local model via Ollama. Its URL is intentionally operator-supplied.
    ollama_url: str = ""
    # gemma3:4b (higher quality) is the default — fast on Apple-Silicon Metal via
    # native arm64 Ollama. Drop to gemma3:1b (DOCVAULT_OLLAMA_MODEL=gemma3:1b) if
    # running on a CPU-only Ollama build.
    ollama_model: str = "gemma3:4b"

    # Anthropic (optional). Key resolves from DOCVAULT_ANTHROPIC_API_KEY,
    # ANTHROPIC_API_KEY, or an `ant auth login` profile.
    anthropic_api_key: str | None = None
    anthropic_url: str = "https://api.anthropic.com"
    rag_model: str = "claude-opus-4-8"

    # Cross-origin API access for local Vite / frontend.
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:80",
        "http://localhost",
    ]

    # ── Multimodal RAG pipeline additions ────────────────────────────────────────

    # Docling / layout-aware parsing (Tier 0)
    # Set to False to fall back to the old Tesseract+pypdf pipeline.
    use_docling: bool = True

    # Qdrant vector DB for hybrid search (Tier 0 → retrieval)
    # Set use_qdrant=False to fall back to SQLite FTS5 (legacy mode).
    use_qdrant: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "docvault"
    # BGE-M3 produces 1024-dim dense + sparse SPLADE vectors.
    qdrant_vector_size: int = 1024

    # BGE-M3 embedding model (dense + sparse hybrid)
    # Loaded lazily; set to "" to disable and stay BM25-only.
    embedding_model: str = "BAAI/bge-m3"

    # Cross-encoder reranker (Tier 1 — async on first query)
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # Number of top Qdrant hits to rerank before sending to the LLM.
    reranker_top_k: int = 20
    visual_reranker_enabled: bool = False
    visual_lane_candidate_budget: int = Field(default=20, ge=1, le=200)
    # Visual generation is an independent, opt-in capability. Search and
    # extraction never depend on it; provider egress remains separately gated.
    visual_generation_enabled: bool = False
    visual_generation_provider: Literal["none", "vllm", "ollama", "anthropic"] = "none"

    # OKF (Open Knowledge Format) bundle directory.
    # If the path exists and contains Markdown files, OKF fast-path is active.
    okf_bundle_dir: Path = BASE_DIR / "okf_bundle"

    # ── Ask AI / Conversational RAG ──────────────────────────────────────────────
    # MongoDB for conversation session persistence (Ask AI feature).
    # Set to "" to disable persistence (stateless mode, history from payload only).
    mongodb_url: str = ""

    # Ask AI conversation settings
    ask_ai_max_history_turns: int = 20   # turns loaded per scope window
    # ── Ask AI runtime budgets (Phase 5). 0 disables the corresponding limit. ──
    ask_ai_max_concurrent_runs: int = 0
    ask_ai_daily_cost_limit_usd: float = 0.0
    ask_ai_daily_token_limit: int = 0
    ask_ai_daily_sandbox_limit: int = 0
    ask_ai_tools_enabled: bool = True
    ask_ai_sandbox_enabled: bool = True
    ask_ai_gdrive_max_docs: int = 5       # hard cap — Google Drive docs to vLLM

    # Chunk size for indexing. Larger chunks mean richer per-passage context but
    # fewer passages per LLM context window. Re-ingest documents after changing.
    rag_chunk_max_chars: int = 3_500
    rag_chunk_overlap_chars: int = 350

    # Google OAuth (for user-level Google Drive integration).
    # Set these in .env / Coolify environment to enable Drive connect in the UI.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Redirect URI must match the registered URI in Google Cloud Console exactly.
    google_redirect_uri: str = "http://localhost:8080/api/v1/auth/google/callback"


    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _validate_trusted_proxy_cidrs(cls, value: object) -> list[str]:
        """Require a bounded list of strict, explicit network CIDRs."""

        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("trusted_proxy_cidrs must be a list")
        if len(value) > 32:
            raise ValueError("trusted_proxy_cidrs contains too many networks")

        canonical: list[str] = []
        for raw_value in value:
            if not isinstance(raw_value, str):
                raise ValueError("trusted proxy CIDR must be a string")
            if not raw_value or raw_value != raw_value.strip() or "/" not in raw_value:
                raise ValueError("trusted proxy CIDR must be an explicit network")
            try:
                network = ipaddress.ip_network(raw_value, strict=True)
            except ValueError:
                raise ValueError("trusted proxy CIDR is invalid") from None
            canonical_value = network.with_prefixlen
            if canonical_value != raw_value:
                raise ValueError("trusted proxy CIDR must be canonical")
            if canonical_value in canonical:
                raise ValueError("trusted proxy CIDR is duplicated")
            canonical.append(canonical_value)
        return canonical

    @field_validator("ocr_languages")
    @classmethod
    def _validate_ocr_languages(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= 4:
            raise ValueError("ocr_languages must contain between one and four codes")
        canonical: list[str] = []
        for language in value:
            if not isinstance(language, str) or re.fullmatch(r"[a-z]{3}", language) is None:
                raise ValueError("OCR language codes must be lowercase ISO/Tesseract codes")
            if language in canonical:
                raise ValueError("OCR language codes must be unique")
            canonical.append(language)
        return canonical

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_archive_uncompressed_bytes(self) -> int:
        return self.max_archive_uncompressed_mb * 1024 * 1024

    @property
    def is_development(self) -> bool:
        return self.environment is RuntimeEnvironment.DEVELOPMENT

    @property
    def is_test(self) -> bool:
        return self.environment is RuntimeEnvironment.TEST

    @property
    def is_production(self) -> bool:
        return self.environment is RuntimeEnvironment.PRODUCTION


class SettingsLoadError(RuntimeError):
    """Stable value-redacted failure raised while parsing environment settings."""

    code = "CFG_SETTINGS_PARSE"

    def __init__(self) -> None:
        super().__init__(
            "DocVault startup configuration rejected (CFG_SETTINGS_PARSE)."
        )


def load_settings() -> Settings:
    """Parse environment settings without propagating value-bearing errors."""

    try:
        return Settings()
    except (SettingsError, ValidationError):
        raise SettingsLoadError() from None


settings = load_settings()
