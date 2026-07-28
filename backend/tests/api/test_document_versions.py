from starlette.testclient import TestClient


def _upload(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/documents",
        files={"file": ("original.txt", b"original document", "text/plain")},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_authorized_version_upload_list_and_download(admin_client: TestClient) -> None:
    original = _upload(admin_client)
    response = admin_client.post(
        f"/api/v1/documents/{original['id']}/versions",
        files={"file": ("revised.txt", b"revised document", "text/plain")},
    )
    assert response.status_code == 202, response.text
    uploaded = response.json()
    assert uploaded["version_id"] != original["version_id"]

    versions = admin_client.get(f"/api/v1/documents/{original['id']}/versions")
    assert versions.status_code == 200, versions.text
    assert [item["version_no"] for item in versions.json()] == [1, 2]

    download = admin_client.get(
        f"/api/v1/documents/{original['id']}/versions/{uploaded['version_id']}/content"
    )
    assert download.status_code == 200
    assert download.content == b"revised document"


def test_version_routes_do_not_disclose_unknown_documents(admin_client: TestClient) -> None:
    response = admin_client.get("/api/v1/documents/999999/versions")
    assert response.status_code == 404
