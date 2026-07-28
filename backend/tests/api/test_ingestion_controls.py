from starlette.testclient import TestClient


def _upload(admin_client: TestClient) -> dict:
    response = admin_client.post(
        "/api/v1/documents",
        files={
            "file": (
                "ingestion-control.txt",
                b"English ingestion control fixture with enough readable text.",
                "text/plain",
            )
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_ingestion_status_cancel_and_retry_contract(
    admin_client: TestClient,
) -> None:
    upload = _upload(admin_client)
    job_id = upload["job_id"]
    assert job_id
    assert upload["version_id"]

    status = admin_client.get(f"/api/v1/ingestions/{job_id}")
    assert status.status_code == 200, status.text
    assert status.json()["state"] == "PENDING"
    assert status.json()["cancellable"] is True
    assert status.json()["terminal"] is False
    assert "DEDUPLICATING" in status.json()["stage_results"]

    cancelled = admin_client.post(f"/api/v1/ingestions/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "CANCELLED"
    assert cancelled.json()["terminal"] is True
    assert cancelled.json()["retryable"] is True

    retried = admin_client.post(f"/api/v1/ingestions/{job_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "PENDING"
    assert retried.json()["terminal"] is False

    conflict = admin_client.post(f"/api/v1/ingestions/{job_id}/retry")
    assert conflict.status_code == 409


def test_unknown_ingestion_is_not_disclosed(admin_client: TestClient) -> None:
    response = admin_client.get(
        "/api/v1/ingestions/00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404
