"""Exact-duplicate listing and resolution workflows."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .. import models, schemas
from ..observability import emit_event
from ..repositories import (
    document_repository,
    duplicate_repository,
    search_repository,
)
from ..repositories.visible_document_repository import (
    VisibleDocumentResolutionUnavailable,
    resolve_visible_document_ids,
)
from ..utils import file_storage
from ..utils.request_context import RequestContext
from . import audit_service, retention_service, search_authorization, search_service
from .exceptions import NotFoundError, ServiceError


def register_exact(
    db: Session,
    *,
    primary_id: int,
    duplicate_id: int,
) -> models.DupGroup:
    group = duplicate_repository.find_open_exact_group(db, primary_id)
    if group is None:
        group = duplicate_repository.create_exact_group(db, primary_id)
        duplicate_repository.add_member(
            db,
            group_id=group.id,
            document_id=primary_id,
        )
    if not duplicate_repository.member_exists(db, group.id, duplicate_id):
        duplicate_repository.add_member(
            db,
            group_id=group.id,
            document_id=duplicate_id,
        )
    return group


def _group_out(
    db: Session,
    group: models.DupGroup,
    *,
    members: list[models.DupMember] | None = None,
    documents: list[models.Document] | None = None,
) -> schemas.DupGroupOut:
    source_members = (
        members
        if members is not None
        else duplicate_repository.list_members(db, group.id)
    )
    if documents is None:
        documents = document_repository.list_by_ids(
            db,
            {member.document_id for member in source_members},
        )
    document_by_id = {document.id: document for document in documents}
    output_members: list[schemas.DupMemberOut] = []
    for member in source_members:
        document = document_by_id.get(member.document_id)
        if document is not None:
            output_members.append(
                schemas.DupMemberOut(
                    document_id=document.id,
                    title=document.title,
                    similarity_score=member.similarity_score,
                )
            )
    return schemas.DupGroupOut(
        id=group.id,
        similarity_type=group.similarity_type,
        primary_document_id=group.primary_document_id,
        resolved=group.resolved,
        members=output_members,
    )


def list_groups(
    db: Session,
    *,
    include_resolved: bool,
    user: models.User,
) -> list[schemas.DupGroupOut]:
    visible_ids = search_authorization.resolve_view_document_ids(db, user)
    return [
        _group_out(db, group, members=members, documents=documents)
        for group, members, documents in duplicate_repository.list_groups_with_visible_members(
            db,
            include_resolved=include_resolved,
            visible_document_ids=visible_ids,
        )
    ]


def list_groups_page(
    db: Session,
    *,
    include_resolved: bool,
    user: models.User,
    after_id: int | None,
    limit: int,
) -> tuple[list[schemas.DupGroupOut], int | None]:
    visible_ids = search_authorization.resolve_view_document_ids(db, user)
    rows = duplicate_repository.list_groups_with_visible_members(
        db,
        include_resolved=include_resolved,
        visible_document_ids=visible_ids,
        after_id=after_id,
        limit=limit + 1,
    )
    outputs = [_group_out(db, group, members=members, documents=documents) for group, members, documents in rows]
    return outputs[:limit], (outputs[limit - 1].id if len(outputs) > limit else None)


def _resolve_ids(
    db: Session,
    user: models.User,
    permission: str,
) -> frozenset[int]:
    """Resolve exact IDs and map malformed policy input to a deny result."""

    try:
        return resolve_visible_document_ids(
            db,
            user_id=user.id,
            permission=permission,
            now=datetime.now(UTC),
        )
    except (VisibleDocumentResolutionUnavailable, ValueError):
        return frozenset()


def resolve_group(
    db: Session,
    user: models.User,
    group_id: int,
    payload: schemas.ResolveDup,
    *,
    context: RequestContext | None = None,
) -> schemas.DupGroupOut:
    group = duplicate_repository.get_group(db, group_id)
    if group is None:
        raise NotFoundError("Duplicate group not found")
    members = duplicate_repository.list_members(db, group_id)
    member_ids = {member.document_id for member in members}
    visible_ids = _resolve_ids(db, user, "VIEW")
    deletable_ids = _resolve_ids(db, user, "DELETE")
    # Do not allow resolving a group to mutate or disclose a member outside
    # the caller's exact VIEW+DELETE document set.  Returning the same safe
    # not-found response also prevents guessed group IDs from becoming an
    # existence oracle.
    if (
        not member_ids
        or not member_ids.issubset(visible_ids)
        or not member_ids.issubset(deletable_ids)
        or group.primary_document_id not in visible_ids
    ):
        raise NotFoundError("Duplicate group not found")
    if payload.primary_document_id not in member_ids:
        raise ServiceError("Primary must be a member of the group")
    if payload.action not in {"keep_primary", "keep_both"}:
        raise ServiceError("Action must be 'keep_primary' or 'keep_both'")

    removed_ids: set[int] = set()
    files_to_delete: list[str] = []
    try:
        group.primary_document_id = payload.primary_document_id
        db.flush()

        if payload.action == "keep_primary":
            removed_ids = member_ids - {payload.primary_document_id}
            documents = document_repository.list_by_ids(db, removed_ids)
            for document in documents:
                retention_service.assert_deletable(document)
            files_to_delete = [
                version.file_key
                for document in documents
                for version in document.versions
                if document_repository.active_references_for_key(
                    db, file_key=version.file_key, excluding_document_id=document.id
                ) == 0
            ]

            duplicate_repository.delete_members_for_documents(db, removed_ids)
            db.flush()
            other_group_ids = duplicate_repository.primary_group_ids_for_documents(
                db,
                removed_ids,
            )
            duplicate_repository.delete_members_for_groups(db, other_group_ids)
            duplicate_repository.delete_groups(db, other_group_ids)
            for document in documents:
                document_repository.delete_metadata(db, document.id)
                search_repository.remove_document(db, document.id)
                document_repository.delete(db, document)

        group.resolved = True
        audit_service.record(
            db,
            actor=user,
            action="DEDUP_RESOLVE",
            object_type="dup_group",
            object_id=group_id,
            details={
                "action": payload.action,
                "primary": payload.primary_document_id,
                "removed": sorted(removed_ids),
            },
            context=context,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    for file_key in files_to_delete:
        try:
            file_storage.delete_file(file_key)
        except Exception as cleanup_error:
            emit_event(
                "duplicate.file_cleanup.failed",
                level=logging.WARNING,
                context=context,
                component="duplicates",
                operation="file_cleanup",
                outcome="error",
                error=cleanup_error,
            )
    for document_id in removed_ids:
        search_service.remove_vector(document_id)

    groups = duplicate_repository.list_groups_with_visible_members(
        db,
        include_resolved=True,
        visible_document_ids=visible_ids,
    )
    for result_group, result_members, result_documents in groups:
        if result_group.id == group_id:
            return _group_out(
                db,
                result_group,
                members=result_members,
                documents=result_documents,
            )
    raise NotFoundError("Duplicate group not found")
