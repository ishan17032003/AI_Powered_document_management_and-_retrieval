"""Deterministic API model defaults and bounded request validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


def test_mutable_response_defaults_are_isolated_per_instance() -> None:
    from app import schemas

    now = datetime.now(timezone.utc)
    pairs = [
        (
            schemas.UserOut(
                id=1,
                username="one",
                name="One",
                email="one@example.test",
                status="active",
            ),
            schemas.UserOut(
                id=2,
                username="two",
                name="Two",
                email="two@example.test",
                status="active",
            ),
            ("roles", "permissions"),
        ),
        (
            schemas.DocumentDetail(
                id=1,
                title="One",
                folder_id=1,
                status="READY",
                ocr_status="native",
                page_count=1,
                created_at=now,
                content_hash="a" * 64,
                language="eng",
            ),
            schemas.DocumentDetail(
                id=2,
                title="Two",
                folder_id=1,
                status="READY",
                ocr_status="native",
                page_count=1,
                created_at=now,
                content_hash="b" * 64,
                language="eng",
            ),
            ("metadata", "versions"),
        ),
        (
            schemas.ImportResult(
                path="source",
                imported=0,
                duplicates=0,
                skipped=0,
                errors=0,
            ),
            schemas.ImportResult(
                path="other",
                imported=0,
                duplicates=0,
                skipped=0,
                errors=0,
            ),
            ("items",),
        ),
        (
            schemas.AskResponse(question="one", answer="one", mode="extractive"),
            schemas.AskResponse(question="two", answer="two", mode="extractive"),
            ("citations", "candidates"),
        ),
        (
            schemas.UserAdminOut(
                id=1,
                username="one",
                name="One",
                email="one@example.test",
                status="active",
            ),
            schemas.UserAdminOut(
                id=2,
                username="two",
                name="Two",
                email="two@example.test",
                status="active",
            ),
            ("roles",),
        ),
    ]

    for first, second, fields in pairs:
        for field in fields:
            first_value = getattr(first, field)
            second_value = getattr(second, field)
            assert first_value is not second_value
            first_value.append("mutation-canary")
            assert second_value == []


@pytest.mark.parametrize(
    ("model_name", "payload"),
    [
        ("ImportRequest", {"path": ""}),
        ("ImportRequest", {"path": "x" * 4097}),
        ("ImportRequest", {"path": "source", "folder_id": 0}),
        ("ImportRequest", {"path": "source", "undeclared": True}),
        ("SemanticQuery", {"q": ""}),
        ("SemanticQuery", {"q": "x" * 2001}),
        ("SemanticQuery", {"q": "query", "limit": 0}),
        ("SemanticQuery", {"q": "query", "limit": 101}),
        ("SemanticQuery", {"q": "query", "undeclared": True}),
        ("AskQuery", {"question": ""}),
        ("AskQuery", {"question": "x" * 4001}),
        ("AskQuery", {"question": "question", "document_id": 0}),
        ("AskQuery", {"question": "question", "undeclared": True}),
        ("OkfEntryCreate", {"filename": "", "content": "body"}),
        ("OkfEntryCreate", {"filename": "x" * 256, "content": "body"}),
        ("OkfEntryCreate", {"filename": "entry.md", "content": ""}),
        (
            "OkfEntryCreate",
            {"filename": "entry.md", "content": "x" * 1_048_577},
        ),
        (
            "OkfEntryCreate",
            {"filename": "entry.md", "content": "body", "undeclared": True},
        ),
        ("ResolveDup", {"primary_document_id": 0}),
        ("ResolveDup", {"primary_document_id": 1, "action": "delete_everything"}),
        ("ResolveDup", {"primary_document_id": 1, "undeclared": True}),
    ],
)
def test_request_models_reject_unbounded_or_undeclared_values(
    model_name: str,
    payload: dict[str, object],
) -> None:
    from app import schemas

    model = getattr(schemas, model_name)
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_request_models_accept_documented_boundaries() -> None:
    from app import schemas

    assert len(schemas.ImportRequest(path="x" * 4096).path) == 4096
    assert schemas.SemanticQuery(q="x" * 2000, limit=100).limit == 100
    assert len(schemas.AskQuery(question="x" * 4000, document_id=1).question) == 4000
    assert (
        len(
            schemas.OkfEntryCreate(
                filename="x" * 255,
                content="x" * 1_048_576,
            ).content
        )
        == 1_048_576
    )
    assert (
        schemas.ResolveDup(primary_document_id=1, action="keep_both").action
        == "keep_both"
    )

    for model in (
        schemas.ImportRequest,
        schemas.SemanticQuery,
        schemas.AskQuery,
        schemas.OkfEntryCreate,
        schemas.ResolveDup,
    ):
        assert model.model_json_schema()["additionalProperties"] is False
