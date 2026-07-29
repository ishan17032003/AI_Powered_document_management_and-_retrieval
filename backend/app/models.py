"""ORM entities — implements the FRD §13 core data model.

Binaries live under settings.storage_dir (the local S3 stand-in); text and
metadata live here in the relational DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .model_base import Base

sql_text = text


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Identity & RBAC ───────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended')",
            name="ck_users_status",
        ),
        CheckConstraint("mfa_enabled IN (0, 1)", name="ck_users_mfa_enabled_bool"),
        Index("ix_users_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(20), default="active"
    )  # active|suspended
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    assignments: Mapped[list["Assignment"]] = relationship(back_populates="user")


class AuthSession(Base):
    """Persisted access/refresh session and token-version state."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint(
            "length(id) BETWEEN 1 AND 128",
            name="ck_auth_sessions_id_bounded",
        ),
        CheckConstraint(
            "token_version >= 0",
            name="ck_auth_sessions_token_version_nonnegative",
        ),
        Index("ix_auth_sessions_user_expiry", "user_id", "expires_at"),
        Index("ix_auth_sessions_revoked_expiry", "revoked_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_auth_sessions_user_id_users")
    )
    refresh_secret_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    token_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user: Mapped[User] = relationship()


class AuthTokenRevocation(Base):
    """Bounded deny-list record for short-lived JWT JTIs."""

    __tablename__ = "auth_token_revocations"
    __table_args__ = (
        CheckConstraint(
            "length(jti) BETWEEN 1 AND 128",
            name="ck_auth_token_revocations_jti_bounded",
        ),
        CheckConstraint(
            "length(reason) BETWEEN 1 AND 120",
            name="ck_auth_token_revocations_reason_bounded",
        ),
        Index("ix_auth_token_revocations_expiry", "expires_at"),
    )

    jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_auth_token_revocations_user_id_users"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(String(120))
    user: Mapped[User | None] = relationship()


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    permissions: Mapped[list["RolePermission"]] = relationship(back_populates="role")


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(
        String(40), unique=True
    )  # VIEW, EDIT_CONTENT, ...


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id"))

    role: Mapped[Role] = relationship(back_populates="permissions")
    permission: Mapped[Permission] = relationship()


class Assignment(Base):
    """Grant a role to a principal at a scope, with ALLOW/DENY effect (FRD §3.2)."""

    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC')",
            name="ck_assignments_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL) OR "
            "(scope_type <> 'GLOBAL' AND scope_id > 0)",
            name="ck_assignments_scope_target",
        ),
        CheckConstraint("effect IN ('ALLOW', 'DENY')", name="ck_assignments_effect"),
        Index("ix_assignments_user_scope", "user_id", "scope_type", "scope_id"),
        Index("ix_assignments_role_scope", "role_id", "scope_type", "scope_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    scope_type: Mapped[str] = mapped_column(
        String(12), default="GLOBAL"
    )  # GLOBAL|CABINET|FOLDER|DOC
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effect: Mapped[str] = mapped_column(String(6), default="ALLOW")  # ALLOW|DENY

    user: Mapped[User] = relationship(back_populates="assignments")
    role: Mapped[Role] = relationship()


# ── Resource access control ──────────────────────────────────────────────────


class Group(Base):
    """A named, archivable collection of USER principals."""

    __tablename__ = "groups"
    __table_args__ = (
        CheckConstraint("id > 0", name="ck_groups_id_positive"),
        CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 160",
            name="ck_groups_name_bounded",
        ),
        CheckConstraint(
            "length(description) <= 500",
            name="ck_groups_description_bounded",
        ),
        CheckConstraint(
            "is_active IN (0, 1)",
            name="ck_groups_is_active_bool",
        ),
        CheckConstraint(
            "created_by > 0",
            name="ck_groups_creator_positive",
        ),
        UniqueConstraint("name", name="uq_groups_name"),
        Index("ix_groups_active_name", "is_active", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_groups_created_by_users")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_now,
        onupdate=_now,
    )

    memberships: Mapped[list["GroupMembership"]] = relationship(back_populates="group")


class GroupMembership(Base):
    """A unique USER membership in one resource-access group."""

    __tablename__ = "group_memberships"
    __table_args__ = (
        CheckConstraint(
            "id > 0",
            name="ck_group_memberships_id_positive",
        ),
        CheckConstraint(
            "group_id > 0",
            name="ck_group_memberships_group_positive",
        ),
        CheckConstraint(
            "user_id > 0",
            name="ck_group_memberships_user_positive",
        ),
        CheckConstraint(
            "created_by > 0",
            name="ck_group_memberships_creator_positive",
        ),
        UniqueConstraint(
            "group_id",
            "user_id",
            name="uq_group_memberships_group_user",
        ),
        Index(
            "ix_group_memberships_user_group",
            "user_id",
            "group_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey(
            "groups.id",
            name="fk_group_memberships_group_id_groups",
        )
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_group_memberships_user_id_users",
        )
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_group_memberships_created_by_users",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    group: Mapped[Group] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    creator: Mapped[User] = relationship(foreign_keys=[created_by])


_ACTIVE_USER_GLOBAL_NO_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'USER'
    AND scope_type = 'GLOBAL'
    AND expires_at IS NULL
    """
)
_ACTIVE_USER_GLOBAL_WITH_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'USER'
    AND scope_type = 'GLOBAL'
    AND expires_at IS NOT NULL
    """
)
_ACTIVE_USER_RESOURCE_NO_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'USER'
    AND scope_type <> 'GLOBAL'
    AND expires_at IS NULL
    """
)
_ACTIVE_USER_RESOURCE_WITH_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'USER'
    AND scope_type <> 'GLOBAL'
    AND expires_at IS NOT NULL
    """
)
_ACTIVE_GROUP_GLOBAL_NO_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'GROUP'
    AND scope_type = 'GLOBAL'
    AND expires_at IS NULL
    """
)
_ACTIVE_GROUP_GLOBAL_WITH_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'GROUP'
    AND scope_type = 'GLOBAL'
    AND expires_at IS NOT NULL
    """
)
_ACTIVE_GROUP_RESOURCE_NO_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'GROUP'
    AND scope_type <> 'GLOBAL'
    AND expires_at IS NULL
    """
)
_ACTIVE_GROUP_RESOURCE_WITH_EXPIRY = text(
    """
    is_active = 1
    AND principal_type = 'GROUP'
    AND scope_type <> 'GLOBAL'
    AND expires_at IS NOT NULL
    """
)


class AccessRule(Base):
    """A permission-level USER/GROUP rule for one resource scope."""

    __tablename__ = "access_rules"
    __table_args__ = (
        CheckConstraint("id > 0", name="ck_access_rules_id_positive"),
        CheckConstraint(
            "principal_type IN ('USER', 'GROUP')",
            name="ck_access_rules_principal_type",
        ),
        CheckConstraint(
            """
            (principal_type = 'USER'
             AND user_id IS NOT NULL
             AND user_id > 0
             AND group_id IS NULL)
            OR
            (principal_type = 'GROUP'
             AND group_id IS NOT NULL
             AND group_id > 0
             AND user_id IS NULL)
            """,
            name="ck_access_rules_principal_target",
        ),
        CheckConstraint(
            "permission_id > 0",
            name="ck_access_rules_permission_positive",
        ),
        CheckConstraint(
            "scope_type IN ('GLOBAL', 'CABINET', 'FOLDER', 'DOC')",
            name="ck_access_rules_scope_type",
        ),
        CheckConstraint(
            """
            (scope_type = 'GLOBAL' AND scope_id IS NULL)
            OR
            (scope_type IN ('CABINET', 'FOLDER', 'DOC')
             AND scope_id IS NOT NULL
             AND scope_id > 0)
            """,
            name="ck_access_rules_scope_target",
        ),
        CheckConstraint(
            "effect IN ('ALLOW', 'DENY')",
            name="ck_access_rules_effect",
        ),
        CheckConstraint(
            "inherits IN (0, 1)",
            name="ck_access_rules_inherits_bool",
        ),
        CheckConstraint(
            "is_active IN (0, 1)",
            name="ck_access_rules_is_active_bool",
        ),
        CheckConstraint(
            "reason IS NULL OR (length(reason) BETWEEN 1 AND 1000 "
            "AND reason = trim(reason))",
            name="ck_access_rules_reason_bounded",
        ),
        CheckConstraint(
            "created_by > 0",
            name="ck_access_rules_creator_positive",
        ),
        Index(
            "ix_access_rules_user_permission_active_expiry",
            "user_id",
            "permission_id",
            "is_active",
            "expires_at",
        ),
        Index(
            "ix_access_rules_group_permission_active_expiry",
            "group_id",
            "permission_id",
            "is_active",
            "expires_at",
        ),
        Index(
            "ix_access_rules_permission_scope_active_expiry",
            "permission_id",
            "scope_type",
            "scope_id",
            "is_active",
            "expires_at",
        ),
        Index(
            "uq_acl_user_global_no_expiry",
            "user_id",
            "permission_id",
            "effect",
            "inherits",
            unique=True,
            sqlite_where=_ACTIVE_USER_GLOBAL_NO_EXPIRY,
        ),
        Index(
            "uq_acl_user_global_with_expiry",
            "user_id",
            "permission_id",
            "effect",
            "inherits",
            "expires_at",
            unique=True,
            sqlite_where=_ACTIVE_USER_GLOBAL_WITH_EXPIRY,
        ),
        Index(
            "uq_acl_user_resource_no_expiry",
            "user_id",
            "permission_id",
            "scope_type",
            "scope_id",
            "effect",
            "inherits",
            unique=True,
            sqlite_where=_ACTIVE_USER_RESOURCE_NO_EXPIRY,
        ),
        Index(
            "uq_acl_user_resource_with_expiry",
            "user_id",
            "permission_id",
            "scope_type",
            "scope_id",
            "effect",
            "inherits",
            "expires_at",
            unique=True,
            sqlite_where=_ACTIVE_USER_RESOURCE_WITH_EXPIRY,
        ),
        Index(
            "uq_acl_group_global_no_expiry",
            "group_id",
            "permission_id",
            "effect",
            "inherits",
            unique=True,
            sqlite_where=_ACTIVE_GROUP_GLOBAL_NO_EXPIRY,
        ),
        Index(
            "uq_acl_group_global_with_expiry",
            "group_id",
            "permission_id",
            "effect",
            "inherits",
            "expires_at",
            unique=True,
            sqlite_where=_ACTIVE_GROUP_GLOBAL_WITH_EXPIRY,
        ),
        Index(
            "uq_acl_group_resource_no_expiry",
            "group_id",
            "permission_id",
            "scope_type",
            "scope_id",
            "effect",
            "inherits",
            unique=True,
            sqlite_where=_ACTIVE_GROUP_RESOURCE_NO_EXPIRY,
        ),
        Index(
            "uq_acl_group_resource_with_expiry",
            "group_id",
            "permission_id",
            "scope_type",
            "scope_id",
            "effect",
            "inherits",
            "expires_at",
            unique=True,
            sqlite_where=_ACTIVE_GROUP_RESOURCE_WITH_EXPIRY,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    principal_type: Mapped[str] = mapped_column(String(5))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_access_rules_user_id_users",
        ),
        nullable=True,
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "groups.id",
            name="fk_access_rules_group_id_groups",
        ),
        nullable=True,
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey(
            "permissions.id",
            name="fk_access_rules_permission_id_permissions",
        )
    )
    scope_type: Mapped[str] = mapped_column(String(7))
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effect: Mapped[str] = mapped_column(String(5))
    inherits: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_access_rules_created_by_users",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_now,
        onupdate=_now,
    )

    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    group: Mapped[Group | None] = relationship()
    permission: Mapped[Permission] = relationship()
    creator: Mapped[User] = relationship(foreign_keys=[created_by])


class AuthorizationPolicyState(Base):
    """The singleton revision used to invalidate authorization decisions."""

    __tablename__ = "authorization_policy_state"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="ck_authorization_policy_state_singleton",
        ),
        CheckConstraint(
            "revision >= 0",
            name="ck_authorization_policy_state_revision_nonnegative",
        ),
        CheckConstraint(
            "updated_by IS NULL OR updated_by > 0",
            name="ck_authorization_policy_state_updater_positive",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_now,
        onupdate=_now,
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_authorization_policy_state_updated_by_users",
        ),
        nullable=True,
    )

    updater: Mapped[User | None] = relationship()


class AclMigrationReport(Base):
    """Reviewable before/after evidence emitted by MIG-008."""

    __tablename__ = "acl_migration_report"
    __table_args__ = (
        Index("ix_acl_migration_report_assignment", "assignment_id"),
        Index("ix_acl_migration_report_outcome", "outcome"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    permission_code: Mapped[str] = mapped_column(String(40))
    scope_type: Mapped[str] = mapped_column(String(12))
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_effect: Mapped[str] = mapped_column(String(6))
    after_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("access_rules.id"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ── Content hierarchy ─────────────────────────────────────────────────────────


class Cabinet(Base):
    __tablename__ = "cabinets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("cabinets.id"), nullable=True
    )


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    cabinet_id: Mapped[int] = mapped_column(ForeignKey("cabinets.id"))
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160))


class DocClass(Base):
    """Taxonomy class (Invoice, Contract, ID, Report, ...)."""

    __tablename__ = "doc_classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("doc_classes.id"), nullable=True
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROCESSING', 'READY', 'REVIEW', 'ERROR')",
            name="ck_documents_status",
        ),
        CheckConstraint(
            "ocr_status IN ('pending', 'native', 'ocr', 'unavailable', 'skipped', 'error')",
            name="ck_documents_ocr_status",
        ),
        CheckConstraint("page_count >= 0", name="ck_documents_page_count_nonnegative"),
        CheckConstraint(
            "lifecycle_state IN ('ACTIVE', 'TOMBSTONED', 'DELETED')",
            name="ck_documents_lifecycle_state",
        ),
        Index(
            "ix_documents_folder_lifecycle_created",
            "folder_id",
            "lifecycle_state",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id"))
    title: Mapped[str] = mapped_column(String(300))
    class_id: Mapped[int | None] = mapped_column(
        ForeignKey("doc_classes.id"), nullable=True
    )
    class_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_hash: Mapped[str] = mapped_column(
        String(64), index=True
    )  # SHA-256 of current version
    status: Mapped[str] = mapped_column(
        String(20), default="PROCESSING"
    )  # PROCESSING|READY|REVIEW|ERROR
    ocr_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending|native|ocr|unavailable|skipped
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str] = mapped_column(String(16), default="eng")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    lifecycle_state: Mapped[str] = mapped_column(
        String(20),
        default="ACTIVE",
        server_default=text("'ACTIVE'"),
    )  # ACTIVE|TOMBSTONED|DELETED
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    legal_hold_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    versions: Mapped[list["DocVersion"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocVersion.version_no",
    )
    doc_class: Mapped[DocClass | None] = relationship()


class DocVersion(Base):
    __tablename__ = "doc_versions"
    __table_args__ = (
        CheckConstraint("version_no > 0", name="ck_doc_versions_version_positive"),
        CheckConstraint("size >= 0", name="ck_doc_versions_size_nonnegative"),
        CheckConstraint(
            "storage_state IN ('STAGED', 'AVAILABLE', 'QUARANTINED', 'MISSING', 'DELETED')",
            name="ck_doc_versions_storage_state",
        ),
        Index(
            "uq_doc_versions_document_version",
            "document_id",
            "version_no",
            unique=True,
        ),
        Index("ix_doc_versions_storage_state", "storage_state", "document_id"),
        Index("ix_doc_versions_document_created", "document_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    file_key: Mapped[str] = mapped_column(String(300))  # relative path in storage
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(
        String(120), default="application/octet-stream"
    )
    size: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64))  # SHA-256
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    storage_state: Mapped[str] = mapped_column(
        String(20),
        default="AVAILABLE",
        server_default=text("'AVAILABLE'"),
    )  # STAGED|AVAILABLE|QUARANTINED|MISSING|DELETED
    extractor_version: Mapped[str] = mapped_column(
        String(40),
        default="legacy-v1",
        server_default=text("'legacy-v1'"),
    )
    chunker_version: Mapped[str] = mapped_column(
        String(40),
        default="legacy-v1",
        server_default=text("'legacy-v1'"),
    )
    embedding_version: Mapped[str] = mapped_column(
        String(80), default="disabled-v1", server_default=text("'disabled-v1'")
    )
    index_version: Mapped[str] = mapped_column(
        String(40), default="fts5-v1", server_default=text("'fts5-v1'")
    )
    extraction_method: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    extractor_name: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    ocr_engine: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ocr_engine_version: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    ocr_languages: Mapped[str | None] = mapped_column(String(40), nullable=True)
    extraction_quality_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    extraction_quality_signals: Mapped[str] = mapped_column(
        Text,
        default="{}",
        server_default=text("'{}'"),
    )
    extraction_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    document: Mapped[Document] = relationship(back_populates="versions")


class VisualAsset(Base):
    """Rebuildable page/image/region derivative with authoritative lineage."""

    __tablename__ = "visual_assets"
    __table_args__ = (
        CheckConstraint(
            "asset_type IN ('PAGE', 'IMAGE', 'REGION', 'THUMBNAIL')",
            name="ck_visual_assets_type",
        ),
        CheckConstraint(
            "lifecycle_state IN ('ACTIVE', 'SUPERSEDED', 'TOMBSTONED', 'DELETED', 'QUARANTINED')",
            name="ck_visual_assets_lifecycle",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="ck_visual_assets_width"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_visual_assets_height"),
        CheckConstraint("size >= 0", name="ck_visual_assets_size_nonnegative"),
        UniqueConstraint("asset_key", name="uq_visual_assets_asset_key"),
        Index("ix_visual_assets_version_state", "version_id", "lifecycle_state"),
        Index("ix_visual_assets_document_page", "document_id", "page_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_key: Mapped[str] = mapped_column(String(128))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("doc_versions.id"))
    asset_type: Mapped[str] = mapped_column(String(12))
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("visual_assets.id"), nullable=True
    )
    file_key: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(120))
    checksum: Mapped[str] = mapped_column(String(64))
    perceptual_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    lifecycle_state: Mapped[str] = mapped_column(
        String(16), default="ACTIVE", server_default=text("'ACTIVE'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class VisualAssetLineage(Base):
    """Auditable edge between a derived visual asset and its source."""

    __tablename__ = "visual_asset_lineage"
    __table_args__ = (
        CheckConstraint(
            "relationship_type IN ('DERIVED_FROM', 'RENDERED_FROM', 'THUMBNAIL_OF', 'REGION_OF')",
            name="ck_visual_asset_lineage_type",
        ),
        UniqueConstraint(
            "asset_id", "source_asset_id", "relationship_type",
            name="uq_visual_asset_lineage_edge",
        ),
        Index("ix_visual_asset_lineage_source", "source_asset_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("visual_assets.id"))
    source_asset_id: Mapped[int] = mapped_column(ForeignKey("visual_assets.id"))
    relationship_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class VisualExtraction(Base):
    """Versioned OCR/caption output; generated descriptions are never trusted."""

    __tablename__ = "visual_extractions"
    __table_args__ = (
        CheckConstraint("output_type IN ('OCR', 'CAPTION', 'DESCRIPTION')", name="ck_visual_extractions_type"),
        CheckConstraint("trusted IN (0, 1)", name="ck_visual_extractions_untrusted_bool"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_visual_extractions_confidence"),
        UniqueConstraint("asset_id", "output_type", "engine_revision", name="uq_visual_extractions_revision"),
        Index("ix_visual_extractions_version_type", "version_id", "output_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("visual_assets.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("doc_versions.id"))
    output_type: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text, default="")
    engine_revision: Mapped[str] = mapped_column(String(160))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str | None] = mapped_column(String(40), nullable=True)
    quality_signals: Mapped[str] = mapped_column(Text, default="{}", server_default=sql_text("'{}'"))
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class VisualProcessingManifest(Base):
    """Idempotent processing state for a document version's visual derivatives."""

    __tablename__ = "visual_processing_manifests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'READY', 'FAILED', 'SUPERSEDED', 'DELETED')",
            name="ck_visual_processing_manifest_state",
        ),
        UniqueConstraint("version_id", "manifest_version", name="uq_visual_processing_manifest_version"),
        Index("ix_visual_processing_manifest_state", "state", "updated_at"),
        Index("ix_visual_processing_manifest_claim", "state", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("doc_versions.id"))
    manifest_version: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(16), default="PENDING", server_default=text("'PENDING'"))
    extractor_revision: Mapped[str] = mapped_column(String(80))
    derivative_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stage: Mapped[str] = mapped_column(String(16), default="VALIDATE", server_default=text("'VALIDATE'"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class VisualRetrievalManifest(Base):
    """Versioned index/model manifest; never the authority for ACL or originals."""

    __tablename__ = "visual_retrieval_manifests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('BUILDING', 'READY', 'CANARY', 'RETIRED', 'FAILED')",
            name="ck_visual_retrieval_manifest_state",
        ),
        UniqueConstraint("lane", "manifest_version", name="uq_visual_retrieval_manifest_version"),
        Index("ix_visual_retrieval_manifest_lane_state", "lane", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lane: Mapped[str] = mapped_column(String(20))
    manifest_version: Mapped[str] = mapped_column(String(80))
    model_revision: Mapped[str] = mapped_column(String(160))
    model_sha256: Mapped[str] = mapped_column(String(64))
    vector_dimension: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(8), default="BUILDING", server_default=text("'BUILDING'"))
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IngestionJob(Base):
    """Durable, idempotent ingestion work item; execution is a later slice."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "length(id) BETWEEN 1 AND 36",
            name="ck_ingestion_jobs_id_bounded",
        ),
        CheckConstraint(
            "state IN "
            "('PENDING', 'RUNNING', 'SUCCEEDED', 'REVIEW', 'FAILED', 'DEAD', "
            "'CANCELLED')",
            name="ck_ingestion_jobs_state",
        ),
        CheckConstraint(
            "length(stage_version) BETWEEN 1 AND 40",
            name="ck_ingestion_jobs_stage_version_bounded",
        ),
        CheckConstraint(
            "stage IN ('EXTRACT', 'INDEX')",
            name="ck_ingestion_jobs_stage",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200",
            name="ck_ingestion_jobs_idempotency_bounded",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_ingestion_jobs_attempts_nonnegative",
        ),
        CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND 80",
            name="ck_ingestion_jobs_error_code_bounded",
        ),
        CheckConstraint(
            "error_message IS NULL OR length(error_message) BETWEEN 1 AND 500",
            name="ck_ingestion_jobs_error_message_bounded",
        ),
        UniqueConstraint("idempotency_key", name="uq_ingestion_jobs_idempotency"),
        Index("ix_ingestion_jobs_state_available", "state", "next_attempt_at"),
        Index("ix_ingestion_jobs_document_state", "document_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", name="fk_ingestion_jobs_document_id_documents"),
        nullable=True,
    )
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("doc_versions.id", name="fk_ingestion_jobs_version_id_doc_versions"),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(
        String(20), default="PENDING", server_default=text("'PENDING'")
    )
    stage_version: Mapped[str] = mapped_column(
        String(40), default="v1", server_default=text("'v1'")
    )
    stage: Mapped[str] = mapped_column(
        String(20), default="EXTRACT", server_default=text("'EXTRACT'")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage_results: Mapped[str] = mapped_column(
        Text,
        default="{}",
        server_default=text("'{}'"),
    )
    degraded_stages: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default=text("'[]'"),
    )
    document: Mapped[Document | None] = relationship()
    version: Mapped[DocVersion | None] = relationship()


class OutboxEvent(Base):
    """Transactional event envelope; dispatch and retries are separate work."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "length(id) BETWEEN 1 AND 36",
            name="ck_outbox_events_id_bounded",
        ),
        CheckConstraint(
            "length(aggregate_type) BETWEEN 1 AND 40",
            name="ck_outbox_events_aggregate_type_bounded",
        ),
        CheckConstraint(
            "length(aggregate_id) BETWEEN 1 AND 80",
            name="ck_outbox_events_aggregate_id_bounded",
        ),
        CheckConstraint(
            "length(event_type) BETWEEN 1 AND 80",
            name="ck_outbox_events_event_type_bounded",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_outbox_events_schema_version_positive",
        ),
        CheckConstraint(
            "length(payload) BETWEEN 2 AND 1048576",
            name="ck_outbox_events_payload_bounded",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200",
            name="ck_outbox_events_idempotency_bounded",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'CLAIMED', 'PROCESSED', 'DEAD')",
            name="ck_outbox_events_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_events_attempts_nonnegative",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 80",
            name="ck_outbox_events_error_code_bounded",
        ),
        CheckConstraint(
            "last_error_message IS NULL OR length(last_error_message) BETWEEN 1 AND 500",
            name="ck_outbox_events_error_message_bounded",
        ),
        UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency"),
        Index("ix_outbox_events_state_available", "state", "available_at"),
        Index(
            "ix_outbox_events_aggregate",
            "aggregate_type",
            "aggregate_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(40))
    aggregate_id: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    payload: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(
        String(12), default="PENDING", server_default=text("'PENDING'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    available_at: Mapped[datetime] = mapped_column(DateTime)
    lock_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dead_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class DocMetadata(Base):
    __tablename__ = "doc_metadata"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


# ── Deduplication ─────────────────────────────────────────────────────────────


class DupGroup(Base):
    __tablename__ = "dup_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    primary_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    similarity_type: Mapped[str] = mapped_column(
        String(12), default="exact"
    )  # exact|near
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class DupMember(Base):
    __tablename__ = "dup_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    dup_group_id: Mapped[int] = mapped_column(ForeignKey("dup_groups.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    similarity_score: Mapped[float] = mapped_column(Float, default=1.0)


# ── Audit ─────────────────────────────────────────────────────────────────────


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_actor_timestamp", "actor_id", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(160), default="")
    action: Mapped[str] = mapped_column(String(60))
    object_type: Mapped[str] = mapped_column(String(40), default="")
    object_id: Mapped[str] = mapped_column(String(40), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
