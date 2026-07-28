"""Isolated HTTP smoke coverage for the current public backend surface."""

from __future__ import annotations

from httpx import Response
from starlette.testclient import TestClient

ALPHA_DOCUMENT = (
    b"Invoice alpha project. The approved invoice total is 42 units. "
    b"This text exists only in the temporary API test vault."
)
DUPLICATE_DOCUMENT = b"Duplicate smoke document with checksum-stable content."


def _expect(response: Response, status_code: int) -> Response:
    assert response.status_code == status_code, response.text
    return response


def _upload(
    client: TestClient,
    *,
    filename: str,
    content: bytes,
) -> dict:
    response = _expect(
        client.post(
            "/api/v1/documents",
            files={"file": (filename, content, "text/plain")},
        ),
        202,
    )
    uploaded = response.json()

    # Upload is intentionally asynchronous. Exercise the production worker
    # boundary before assertions that depend on extraction or indexing.
    from app import database
    from app.repositories import job_repository
    from app.services import ingestion_worker

    db = database.SessionLocal()
    try:
        claimed = job_repository.claim_ingestion_job(
            db,
            owner=f"api-smoke-{uploaded['job_id']}",
        )
        assert claimed is not None
        assert claimed.id == uploaded["job_id"]
        db.commit()
        completed = ingestion_worker.run_claimed_job(db, claimed)
        assert completed.state == "SUCCEEDED"
        db.refresh(completed.document)
        uploaded["status"] = completed.document.status
        uploaded["ocr_status"] = completed.document.ocr_status
    finally:
        db.close()
    return uploaded


def test_authentication_and_admin_smoke(
    api_client: TestClient,
    demo_credentials: dict[str, str],
) -> None:
    _expect(api_client.get("/api/v1/admin/users"), 401)
    _expect(
        api_client.post(
            "/api/v1/auth/login",
            data={"username": "admin", "password": "wrong-password"},
        ),
        401,
    )

    login = _expect(
        api_client.post(
            "/api/v1/auth/login",
            data={
                "username": "admin",
                "password": demo_credentials["admin"],
            },
        ),
        200,
    ).json()
    assert login["token_type"] == "bearer"
    api_client.headers["Authorization"] = f"Bearer {login['access_token']}"

    me = _expect(api_client.get("/api/v1/auth/me"), 200).json()
    assert me["username"] == "admin"
    assert "Super Admin" in me["roles"]
    assert "ADMIN" in me["permissions"]

    users = _expect(api_client.get("/api/v1/admin/users"), 200).json()
    assert {user["username"] for user in users} == {
        "admin",
        "auditor",
        "contributor",
        "viewer",
    }

    matrix = _expect(api_client.get("/api/v1/admin/rbac-matrix"), 200).json()
    assert "VIEW" in matrix["permissions"]
    assert "ADMIN" in matrix["roles"]["Super Admin"]

    stats = _expect(api_client.get("/api/v1/admin/stats"), 200).json()
    assert stats["total_documents"] == 0
    assert stats["storage_bytes"] == 0


def test_server_folder_import_is_disabled_by_default(
    admin_client: TestClient,
) -> None:
    raw_path = "/server/private/import-path-canary"
    response = admin_client.post(
        "/api/v1/documents/import-folder",
        json={"path": raw_path, "recursive": True},
    )

    assert response.status_code == 403
    assert response.json()["message"] == "Server folder import is disabled"
    assert raw_path not in response.text


def test_unbounded_or_undeclared_api_inputs_are_rejected(
    admin_client: TestClient,
) -> None:
    requests = [
        admin_client.get(
            "/api/v1/search",
            params={"q": "x" * 2001},
        ),
        admin_client.post(
            "/api/v1/search/semantic",
            json={"q": "query", "limit": 0},
        ),
        admin_client.post(
            "/api/v1/search/ask",
            json={"question": "", "undeclared": True},
        ),
        admin_client.post(
            "/api/v1/documents/import-folder",
            json={"path": "", "recursive": True},
        ),
        admin_client.post(
            "/api/v1/search/okf/entries",
            json={"filename": "entry.md", "content": ""},
        ),
        admin_client.post(
            "/api/v1/duplicates/1/resolve",
            json={"primary_document_id": 1, "action": "delete_everything"},
        ),
        admin_client.get("/api/v1/documents", params={"limit": 0}),
        admin_client.get("/api/v1/audit", params={"action": "x" * 61}),
        admin_client.get("/api/v1/documents/-1"),
    ]

    assert [response.status_code for response in requests] == [422] * len(requests)


def test_document_lifecycle_search_and_ask_smoke(
    admin_client: TestClient,
) -> None:
    uploaded = _upload(
        admin_client,
        filename="alpha-invoice.txt",
        content=ALPHA_DOCUMENT,
    )
    document_id = uploaded["id"]
    assert uploaded["status"] == "READY"
    assert uploaded["ocr_status"] == "native"
    assert uploaded["duplicate_of"] is None

    listed = _expect(admin_client.get("/api/v1/documents"), 200).json()
    assert [document["id"] for document in listed] == [document_id]

    detail = _expect(
        admin_client.get(f"/api/v1/documents/{document_id}"),
        200,
    ).json()
    assert detail["title"] == "alpha-invoice.txt"
    assert "invoice total is 42" in detail["ocr_text"].lower()
    assert len(detail["versions"]) == 1

    download = _expect(
        admin_client.get(f"/api/v1/documents/{document_id}/content"),
        200,
    )
    assert download.content == ALPHA_DOCUMENT
    assert "alpha-invoice.txt" in download.headers["content-disposition"]

    keyword = _expect(
        admin_client.get("/api/v1/search", params={"q": "alpha"}),
        200,
    ).json()
    assert keyword["mode"] == "keyword"
    assert [hit["document_id"] for hit in keyword["hits"]] == [document_id]

    semantic = _expect(
        admin_client.post(
            "/api/v1/search/semantic",
            json={"q": "alpha", "limit": 10},
        ),
        200,
    ).json()
    assert semantic["mode"] == "semantic"
    assert [hit["document_id"] for hit in semantic["hits"]] == [document_id]

    answer = _expect(
        admin_client.post(
            "/api/v1/search/ask",
            json={
                "question": "What is the approved invoice total?",
                "document_id": document_id,
            },
        ),
        200,
    ).json()
    assert answer["mode"] == "extractive"
    assert answer["scoped_document_id"] == document_id
    assert answer["citations"][0]["document_id"] == document_id
    assert "42 units" in answer["answer"]

    _expect(admin_client.delete(f"/api/v1/documents/{document_id}"), 204)
    _expect(admin_client.get(f"/api/v1/documents/{document_id}"), 404)
    assert _expect(admin_client.get("/api/v1/documents"), 200).json() == []


def test_duplicates_and_audit_smoke(admin_client: TestClient) -> None:
    first = _upload(
        admin_client,
        filename="duplicate-a.txt",
        content=DUPLICATE_DOCUMENT,
    )
    second = _upload(
        admin_client,
        filename="duplicate-b.txt",
        content=DUPLICATE_DOCUMENT,
    )
    assert second["duplicate_of"] == first["id"]

    groups = _expect(admin_client.get("/api/v1/duplicates"), 200).json()
    assert len(groups) == 1
    group = groups[0]
    assert group["resolved"] is False
    assert {member["document_id"] for member in group["members"]} == {
        first["id"],
        second["id"],
    }

    resolved = _expect(
        admin_client.post(
            f"/api/v1/duplicates/{group['id']}/resolve",
            json={
                "primary_document_id": first["id"],
                "action": "keep_both",
            },
        ),
        200,
    ).json()
    assert resolved["resolved"] is True
    assert _expect(admin_client.get("/api/v1/duplicates"), 200).json() == []
    assert (
        len(
            _expect(
                admin_client.get(
                    "/api/v1/duplicates",
                    params={"include_resolved": True},
                ),
                200,
            ).json()
        )
        == 1
    )

    audit = _expect(
        admin_client.get("/api/v1/audit", params={"limit": 100}),
        200,
    ).json()
    actions = [entry["action"] for entry in audit]
    assert actions.count("UPLOAD") == 2
    assert "DEDUP_RESOLVE" in actions
    assert "LOGIN" in actions
