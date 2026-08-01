"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestModel(BaseModel):
    """Reject undeclared request data instead of silently discarding it."""

    model_config = ConfigDict(extra="forbid")


# ── Auth ──────────────────────────────────────────────────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


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
class AskQuery(RequestModel):
    question: str = Field(min_length=1, max_length=4000)
    document_id: int | None = Field(default=None, gt=0)


class Citation(BaseModel):
    index: int
    document_id: int
    title: str


class Candidate(BaseModel):
    document_id: int
    title: str


class AskResponse(BaseModel):
    question: str
    answer: str
    mode: str  # claude | extractive | clarify
    model: str | None = None
    needs_clarification: bool = False
    scoped_document_id: int | None = None
    citations: list[Citation] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    images: list[VisualSearchHit] = Field(default_factory=list)


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

