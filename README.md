#  DocVault™ — Local Build

AI-based Digital Document Management System (DDMS) with OCR, full-text search,
deduplication, Role-Based Access Control and an immutable audit trail.

This repository is the **local-first vertical slice** of the platform described in the
BRD / FRD / Solution Architecture. It runs entirely on your machine (FastAPI + SQLite +
Tesseract) with no AWS dependency, and is structured so the same code can later target
Postgres / OpenSearch / SageMaker as the spec prescribes.

## What works in this slice

| Capability | Spec ref | Status |
|---|---|---|
| JWT login + native accounts | FR-SEC-01 | ✅ |
| Role → Permission → Resource RBAC (DENY beats ALLOW) | FRD §3.2/3.3 | ✅ |
| Multi-format upload (PDF/image/txt/office) | FR-CAP-01 | ✅ |
| Hybrid OCR routing: native PDF text vs Tesseract on scans | FR-GL-01 / FR-OCR-01 | ✅ |
| Auto-classification (rule/keyword heuristic, pluggable) | FR-OCR-02 | ✅ (v0) |
| Full-text search, security-trimmed + highlighted | FR-IDX-01/02 | ✅ (SQLite FTS5) |
| Semantic search endpoint (falls back to FTS if no model) | FR-IDX-03 | ✅ (fallback) |
| Text → image/page visual search + ephemeral image query | MM-020…MM-027 | ✅ lexical default; optional local SigLIP2 lane |
| **Ask AI** — natural-language Q&A over all docs, with citations (RAG) | FR-IDX-03 / Arch §7.2 | ✅ |
| Exact duplicate detection (SHA-256) + resolve | FR-DUP-01/03 | ✅ |
| Document detail: metadata, OCR text, versions | S5 | ✅ |
| Immutable audit trail of every action | FR-AUD-01 | ✅ |
| Web SPA (React + TypeScript) | §15 | ✅ core screens |

Later phases (semantic embeddings at scale, workflow, retention/legal-hold, GraphRAG,
multi-tenancy, SageMaker) remain documented or gated. The optional visual semantic
lane requires the separate `visual` extra, a locally staged digest-verified SigLIP2
artifact, and explicit `DOCVAULT_VISUAL_SEMANTIC_*` flags; it never downloads a model
at request time.

## Architecture (local)

```
frontend/  React + Vite + TypeScript SPA  ──HTTP/JWT──►  backend/  FastAPI
                                                          ├─ SQLite (metadata, RBAC, audit)
                                                          ├─ SQLite FTS5 (full-text index)
                                                          ├─ Tesseract OCR (scanned pages)
                                                          └─ ./storage (object store = S3 stand-in)
```

## Quick start

### 1. Backend

```bash
cd backend
./start.sh
```

The script requires `uv 0.11.9`, installs from the frozen `uv.lock`, explicitly
migrates and seeds an ignored `.runtime/` data directory, and starts Uvicorn. Its
automatic migration guard accepts only the script-generated default SQLite target
below a non-broad `DOCVAULT_RUNTIME_DIR`, and refuses nonempty unversioned/unknown
targets. Custom/external targets must be migrated separately. It does not write to the
checked-in baseline database or storage. It declares
`DOCVAULT_ENVIRONMENT=development`; tests use `test`, while Docker and Compose declare
`production`.

The default install is the lightweight, offline profile. To install the full
Docling/Qdrant/embedding provider stack, start with
`DOCVAULT_INSTALL_AI=1 ./start.sh`.
To install only the local SigLIP2 runtime for visual semantic search, use
`DOCVAULT_INSTALL_VISUAL=1 ./start.sh`; model weights are still staged separately
and are never downloaded by the API.

Run backend tests from the same frozen environment with:

```bash
cd backend
uv sync --frozen
uv run --frozen pytest
```

Verify that the generated HTTP contract still matches the reviewed snapshot with:

```bash
cd backend
uv run --frozen python -m scripts.export_openapi --check
```

For an intentional API change, classify and review it under
[`docs/api-contract-policy.md`](docs/api-contract-policy.md), then regenerate the
snapshot with `uv run --frozen python -m scripts.export_openapi`.

Run the same quality and coverage gates declared by backend CI with:

```bash
cd backend
uv run --frozen ruff format --check app scripts tests
uv run --frozen ruff check app scripts tests
uv run --frozen mypy
uv run --frozen python -m scripts.export_openapi --check
uv run --frozen pytest -q \
  --cov=app --cov-branch --cov-report=term-missing \
  --cov-report=xml:coverage.xml --cov-fail-under=60
```

The required-check names and repository protection steps are documented in
[`docs/ci-policy.md`](docs/ci-policy.md). The pinned secret, dependency, SAST, and
container gates—including the exact, expiring risk-acceptance contract—are documented in
[`docs/security-scan-policy.md`](docs/security-scan-policy.md).

The three accepted backend runtime modes and their current safety boundary are defined
in [`docs/runtime-environment-contract.md`](docs/runtime-environment-contract.md).
The fail-closed production rules, redacted error contract, and operator flow are in
[`docs/production-configuration-policy.md`](docs/production-configuration-policy.md).
The disabled-by-default server-folder import boundary and approved-root deployment
contract are in
[`docs/server-folder-import-policy.md`](docs/server-folder-import-policy.md).

The checked-in Docker/Compose defaults intentionally fail production validation until
the required PostgreSQL URL, signing key, HTTPS browser origin, and other approved
values are injected. A successful image build is not a production configuration.

API docs (Swagger): http://localhost:8000/docs

**OCR requires the Tesseract binary.** On macOS: `brew install tesseract`.
If it is missing, uploads still succeed but scanned pages are marked
`ocr: unavailable` instead of being read.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

App: http://localhost:5173

### Ask AI — answer generation (local Gemma, Claude, or extractive)

The **Ask AI** page retrieves the most relevant passages across every document you can
access (RBAC-trimmed), resolves *which* document you mean (asking you when ambiguous),
and composes an answer with `[n]` citations. The answer is generated by one of three
providers, selected by `DOCVAULT_LLM_PROVIDER` (default `none`):

| Provider | How to enable | Notes |
|---|---|---|
| **Extractive (default)** | `DOCVAULT_LLM_PROVIDER=none` | No model and no provider request; returns cited passages. |
| **Local Gemma (Ollama)** | Select `ollama`, opt in, and allowlist the exact configured host | No API key. Model via `DOCVAULT_OLLAMA_MODEL`. |
| **Claude (Anthropic)** | Select `anthropic`, opt in, allowlist `api.anthropic.com`, and inject the API key | Model via `DOCVAULT_RAG_MODEL` (default `claude-opus-4-8`). |

The safe default is `none`. A networked provider also requires
`DOCVAULT_ALLOW_EXTERNAL_LLM=true`, an exact JSON host list in
`DOCVAULT_LLM_ALLOWED_HOSTS`, and an explicit provider URL where applicable. Automatic
provider routing is unsupported in every runtime. Retrieval is RBAC-trimmed before generation,
but production release still depends on completing the resource-authorization and RAG
safety work packages.

Provider work is also fail-bounded by default: a 3-second connect timeout, 20-second
read timeout, 30-second total web-request wait, 512 output tokens, 32 KiB encoded
document context, and two concurrent provider calls per backend process. Saturated calls
are not queued and provider failure or timeout returns the extractive answer. Configure
these through the corresponding `DOCVAULT_RAG_PROVIDER_*` variables and
`DOCVAULT_RAG_MAX_CONTEXT_BYTES`; invalid or incoherent limits fail startup.

**Performance note (local Gemma):** on Apple Silicon, make sure Ollama is the **native
arm64 build** so it uses the Metal GPU — an x86 build under Rosetta runs models on CPU
(~1 min/answer). With Metal, `gemma3:4b` answers in a few seconds. Choose the model to fit
your hardware: `gemma3:1b` (fast, CPU-friendly) → `gemma3:4b` (higher quality, needs GPU).

### Development demo accounts

The explicit development seed creates `admin`, `contributor`, `viewer`, and `auditor`
only in the isolated local runtime. Strong random passwords are printed once when each
identity is first created; no demo password exists in source, image, Compose, or
documentation. A later restart does not change or reprint existing credentials.

Production never creates demo identities. Provision its first administrator through the
reviewed one-time flow in
[`docs/runbooks/initial-administrator-provisioning.md`](docs/runbooks/initial-administrator-provisioning.md).

## Repository layout

```
backend/
  app/
    main.py          app wiring, CORS, router mounts
    config.py        settings (env-overridable)
    database.py      SQLAlchemy engine/session lifecycle (schema is Alembic-owned)
    models.py        ORM entities (FRD §13 data model)
    schemas.py       Pydantic request/response models
    security.py      password hashing + JWT
    rbac.py          permission catalogue + enforcement (DENY>ALLOW, scope inheritance)
    audit.py         audit-log helper
    ocr.py           hybrid OCR / text extraction pipeline
    dedup.py         SHA-256 duplicate detection
    search.py        FTS5 indexing + query, security trimming
    classify.py      lightweight auto-classifier (pluggable)
    seed.py          bootstrap roles/permissions/users/cabinet
    routers/         auth, documents, search, duplicates, admin, audit
frontend/
  src/
    api.ts           typed API client
    auth.tsx         auth context + token storage
    pages/           Login, Dashboard, Upload, Search, DocumentDetail, Duplicates, Audit
    components/      Layout / nav
```

## Mapping to the full Solution Architecture

| Local slice | Production target (Solution Architecture) |
|---|---|
| SQLite | Aurora PostgreSQL (metadata/RBAC/audit) |
| SQLite FTS5 | OpenSearch (FTS + k-NN hybrid) |
| Local `./storage` dir | S3 + Object Lock (WORM), KMS/BYOK |
| Tesseract (CPU) | PaddleOCR-VL / DeepSeek-OCR on SageMaker GPU |
| Keyword fallback semantic | Qwen3-VL + Qwen 3.5 RAG, BGE embeddings |
| In-process job | SQS + EventBridge async pipeline |

The service boundaries in `routers/` mirror the production service decomposition so each
can be split into its own deployable later.
