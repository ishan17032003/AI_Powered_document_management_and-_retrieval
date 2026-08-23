"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequestModel(BaseModel):
    """Reject undeclared request data instead of silently discarding it."""

    model_config = ConfigDict(extra="forbid")


# ── Auth ──────────────────────────────────────────────────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class RoleGroupMap(BaseModel):
    role_name: str
    group_id: int



class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    email: str
    status: str
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


# ── Documents ─────────────────────────────────────────────────────────────────
class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version_no: int
    filename: str
    content_type: str
    size: int
    checksum: str
    created_at: datetime


class MetadataOut(BaseModel):
    key: str
    value: str
    confidence: float | None = None


class MetadataUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=500)
    confidence: float | None = Field(default=None, ge=0, le=1)


class DocumentMetadataUpdate(RequestModel):
    expected_updated_at: datetime
    metadata: list[MetadataUpdate] = Field(max_length=100)


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    folder_id: int
    doc_class: str | None = None
    class_confidence: float | None = None
    status: str
    ocr_status: str
    ocr_confidence: float | None = None
    page_count: int
    size: int | None = None
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentSummary):
    content_hash: str
    language: str
    ocr_text: str = ""
    metadata: list[MetadataOut] = Field(default_factory=list)
    versions: list[VersionOut] = Field(default_factory=list)
    is_duplicate_of: int | None = None


class UploadResult(BaseModel):
    id: int
    job_id: str | None = None
    version_id: int | None = None
    title: str
    status: str
    folder_id: int
    ocr_status: str
    doc_class: str | None = None
    duplicate_of: int | None = None
    pipeline: dict[str, object]


class ExtractionProvenanceOut(BaseModel):
    method: str | None = None
    extractor_name: str | None = None
    extractor_version: str | None = None
    ocr_engine: str | None = None
    ocr_engine_version: str | None = None
    ocr_languages: list[str] = Field(default_factory=list)
    quality_score: float | None = None
    quality_signals: dict[str, bool | float | int | str] = Field(
        default_factory=dict
    )
    completed_at: datetime | None = None


class IngestionStatusOut(BaseModel):
    id: str
    document_id: int | None = None
    version_id: int | None = None
    state: str
    stage: str
    stage_version: str
    attempt_count: int
    retryable: bool
    cancellable: bool
    terminal: bool
    next_attempt_at: datetime | None = None
    error_code: str | None = None
    document_status: str | None = None
    stage_results: dict[str, dict[str, object]] = Field(default_factory=dict)
    degraded_stages: list[str] = Field(default_factory=list)
    extraction: ExtractionProvenanceOut | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class MoveDocument(RequestModel):
    folder_id: int = Field(gt=0)
    expected_updated_at: datetime


# ── Operator-approved server-folder import ────────────────────────────────────
class ImportRequest(RequestModel):
    path: str = Field(min_length=1, max_length=4096)
    recursive: bool = True
    folder_id: int | None = Field(default=None, gt=0)


class ImportItem(BaseModel):
    filename: str
    status: str  # imported | duplicate | skipped | error
    document_id: int | None = None
    detail: str | None = None


class ImportResult(BaseModel):
    path: str
    imported: int
    duplicates: int
    skipped: int
    errors: int
    items: list[ImportItem] = Field(default_factory=list)


# ── Search ────────────────────────────────────────────────────────────────────
class SearchMatchRange(BaseModel):
    """Half-open character range in the returned plain-text snippet."""

    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SearchHit(BaseModel):
    document_id: int
    title: str
    doc_class: str | None = None
    snippet: str
    score: float
    match_ranges: list[SearchMatchRange] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    mode: str  # keyword | semantic
    count: int
    hits: list[SearchHit]


class DocumentPage(BaseModel):
    """Bounded cursor page for clients that need stable document traversal."""

    items: list[DocumentSummary]
    next_cursor: str | None = None


class CursorPage(BaseModel):
    """Common bounded page envelope for stable list traversal."""

    items: list[object]
    next_cursor: str | None = None


class AccessRuleCreate(RequestModel):
    principal_type: Literal["USER", "GROUP"]
    principal_id: int = Field(gt=0)
    permission: str = Field(min_length=1, max_length=40)
    effect: Literal["ALLOW", "DENY"]
    inherits: bool = False
    expires_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=1000)


class AccessRuleOut(BaseModel):
    id: int
    principal_type: str
    principal_id: int
    permission: str
    scope_type: str
    scope_id: int | None
    effect: str
    inherits: bool
    is_active: bool
    expires_at: datetime | None
    reason: str | None
    created_by: int
    created_at: datetime


class AccessRulePage(BaseModel):
    items: list[AccessRuleOut]
    next_cursor: str | None = None


class EffectiveAccessRule(BaseModel):
    id: int
    principal_type: str
    principal_id: int
    effect: str
    inherits: bool
    reason: str | None
    scope_type: str
    scope_id: int | None


class EffectiveAccessOut(BaseModel):
    user_id: int
    permission: str
    scope_type: str
    scope_id: int | None
    allowed: bool
    reason_code: str
    matched_rule: EffectiveAccessRule | None = None
    policy_version: int
    ancestry: list[dict[str, int | str | None]]


class SemanticQuery(RequestModel):
    q: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=20, ge=1, le=100)


class VisualSearchQuery(RequestModel):
    """Bounded text-to-visual query contract."""

    q: str = Field(min_length=1, max_length=2000)
    mode: Literal["text_to_image", "text_to_page", "hybrid"] = "text_to_image"
    limit: int = Field(default=20, ge=1, le=100)


class VisualImageSearchQuery(RequestModel):
    """Ephemeral image-to-image semantic query contract."""

    mode: Literal["image_to_image", "hybrid"] = "image_to_image"
    limit: int = Field(default=20, ge=1, le=100)


class VisualSearchHit(BaseModel):
    asset_id: int
    document_id: int
    version_id: int
    title: str
    asset_type: Literal["PAGE", "IMAGE", "REGION", "THUMBNAIL"]
    result_type: Literal["page", "image"]
    page_number: int | None = None
    content_type: str
    snippet: str
    score: float
    matched_lanes: list[str] = Field(default_factory=list)
    preview_available: bool = True


class VisualSearchResponse(BaseModel):
    query: str
    mode: Literal["text_to_image", "text_to_page", "image_to_image", "hybrid"]
    count: int
    hits: list[VisualSearchHit]
    provider: str
    degraded: bool = False


class VisualFeedbackRequest(RequestModel):
    result_id: str = Field(min_length=1, max_length=128)
    category: Literal["relevant", "irrelevant", "unauthorized", "unsafe", "wrong_type"]
    note: str | None = Field(default=None, max_length=240)


# ── Ask AI (RAG) ──────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ModelSelection(RequestModel):
    """One requested model run: provider key + optional version id."""

    provider: str = Field(min_length=1, max_length=32)
    model_id: str | None = Field(default=None, max_length=128)


class AskQuery(RequestModel):
    question: str = Field(min_length=1, max_length=4000)
    # Per-run model selection; None = every configured provider at default version
    models: list[ModelSelection] | None = Field(default=None, max_length=8)
    document_id: int | None = Field(default=None, gt=0)
    history: list[ChatMessage] | None = Field(default=None, max_length=20)
    # Conversational session management
    conversation_id: str | None = Field(default=None, max_length=64)
    company_kb_enabled: bool = True
    google_drive_enabled: bool = False

    @model_validator(mode="after")
    def _ensure_source_selection(self) -> "AskQuery":
        # At least one source must be selected; if neither is selected, default company KB to True
        if not self.company_kb_enabled and not self.google_drive_enabled:
            self.company_kb_enabled = True
        return self


class Citation(BaseModel):
    index: int
    document_id: int
    title: str


class Candidate(BaseModel):
    document_id: int
    title: str


class ScopedDocumentInfo(BaseModel):
    document_id: int
    title: str


class ScopedClassInfo(BaseModel):
    class_id: int
    class_name: str
    document_ids: list[int] = Field(default_factory=list)


class ActiveScopeInfo(BaseModel):
    documents: list[ScopedDocumentInfo] = Field(default_factory=list)
    classes: list[ScopedClassInfo] = Field(default_factory=list)


class DriveSourceCitation(BaseModel):
    id: str | None = None
    name: str | None = None
    mimeType: str | None = None
    webViewLink: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    snippet: str = ""


class AskResponse(BaseModel):
    question: str
    answer: str
    mode: str  # claude | extractive | clarify | gdrive | combined
    model: str | None = None
    needs_clarification: bool = False
    scoped_document_id: int | None = None
    citations: list[Citation] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    images: list[VisualSearchHit] = Field(default_factory=list)
    # New session & scope metadata
    conversation_id: str | None = None
    active_scope: ActiveScopeInfo | None = None
    sources_used: dict[str, bool] | None = None
    drive_sources: list[DriveSourceCitation] | None = None


# ── Conversation Management Schemas ──────────────────────────────────────────
class ConversationCreateIn(RequestModel):
    title: str | None = Field(default=None, max_length=120)
    company_kb_enabled: bool = True
    google_drive_enabled: bool = False


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    last_message_at: datetime
    company_kb_enabled: bool = True
    google_drive_enabled: bool = False


class ConversationMessageOut(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    sources_used: dict[str, bool] | None = None


class ConversationDetailOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    last_message_at: datetime
    active_scope: ActiveScopeInfo
    company_kb_enabled: bool = True
    google_drive_enabled: bool = False
    messages: list[ConversationMessageOut] = Field(default_factory=list)



# ── OKF knowledge management ──────────────────────────────────────────────────
class OkfEntryCreate(RequestModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=1_048_576)


# ── Duplicates ────────────────────────────────────────────────────────────────
class DupMemberOut(BaseModel):
    document_id: int
    title: str
    similarity_score: float


class DupGroupOut(BaseModel):
    id: int
    similarity_type: str
    primary_document_id: int
    resolved: bool
    members: list[DupMemberOut]


class ResolveDup(RequestModel):
    primary_document_id: int = Field(gt=0)
    action: Literal["keep_primary", "keep_both"] = "keep_primary"


# ── Audit ─────────────────────────────────────────────────────────────────────
class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_name: str
    action: str
    object_type: str
    object_id: str
    ip: str
    timestamp: datetime
    details: str


# ── Admin ─────────────────────────────────────────────────────────────────────
class UserAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    email: str
    status: str
    roles: list[str] = Field(default_factory=list)


class AuditPage(BaseModel):
    items: list[AuditOut]
    next_cursor: str | None = None


class DuplicatePage(BaseModel):
    items: list[DupGroupOut]
    next_cursor: str | None = None


class UserPage(BaseModel):
    items: list[UserAdminOut]
    next_cursor: str | None = None


class JobOut(BaseModel):
    id: str
    state: str
    stage_version: str
    idempotency_key: str
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    document_id: int | None = None
    version_id: int | None = None
    stage: str = "EXTRACT"
    stage_results: dict[str, object] = Field(default_factory=dict)
    degraded_stages: list[str] = Field(default_factory=list)


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = None


class AdminUserCreate(RequestModel):
    username: str = Field(min_length=3, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=16, max_length=256)
    role: str = Field(default="Viewer", min_length=1, max_length=80)


class AdminUserPatch(RequestModel):
    status: Literal["active", "suspended"] | None = None


class AdminRoleAssignment(RequestModel):
    role: str = Field(min_length=1, max_length=80)


class AdminCredentialReset(RequestModel):
    password: str = Field(min_length=16, max_length=256)


# ── Document reclassification (Super Admin only) ──────────────────────────────

class ReclassifyDocument(RequestModel):
    """Super Admin body for manually changing a document's class.

    Either ``class_id`` (existing class) **or** ``class_name`` (new or
    existing name, looked up / created on the fly) may be supplied.
    Supplying neither clears the class (sets to Uncategorized).
    """

    class_id: int | None = Field(default=None, gt=0)
    class_name: str | None = Field(default=None, min_length=1, max_length=80)


class DocClassOut(BaseModel):
    """Public projection of a DocClass taxonomy entry."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_id: int | None = None


# ── User-centric document access view (Admin/Super Admin) ────────────────────

class UserDocRuleOut(BaseModel):
    """One access_rule row surfaced from a user's perspective.

    Includes revoked (is_active=False) rules so Super Admin can see
    the full history. All permissions (VIEW, DOWNLOAD, …) are included.
    """

    rule_id: int
    doc_id: int
    doc_title: str
    doc_class: str | None
    effect: str          # ALLOW | DENY
    permission: str      # VIEW | DOWNLOAD | DELETE | …
    reason: str | None
    is_active: bool
    created_at: datetime


class AllDocRuleOut(BaseModel):
    """Flat projection of a DOC-scoped access rule with user identity.

    Used by the admin access-control matrix view to display every
    user → document rule in one table.
    """

    rule_id: int
    user_id: int
    user_name: str
    user_username: str
    doc_id: int
    doc_title: str
    doc_class: str | None
    effect: str
    permission: str
    reason: str | None
    is_active: bool
    created_at: datetime


# ── System Settings (Admin) ───────────────────────────────────────────────────

class VllmUrlUpdate(RequestModel):
    """Payload to update the vLLM URL dynamically."""
    vllm_url: str


class VllmUrlOut(BaseModel):
    """Response containing the updated vLLM URL."""
    vllm_url: str

class AllGroupDocRuleOut(BaseModel):
    rule_id: int
    group_id: int
    group_name: str
    doc_id: int
    doc_title: str
    doc_class: str | None
    effect: str
    permission: str
    reason: str | None
    is_active: bool
    created_at: datetime
