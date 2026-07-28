"""Regression tests for the dependency-free container liveness probe."""

from __future__ import annotations

import http.client
from dataclasses import dataclass

import pytest

from app import container_healthcheck


@dataclass
class FakeResponse:
    status: int
    read_called: bool = False

    def read(self) -> bytes:
        self.read_called = True
        return b'{"status":"live"}'


class FakeConnection:
    response_status = 200
    fail_request = False
    latest: FakeConnection | None = None

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request_args: tuple[str, str] | None = None
        self.closed = False
        self.response = FakeResponse(self.response_status)
        type(self).latest = self

    def request(self, method: str, path: str) -> None:
        if self.fail_request:
            raise OSError("connection refused")
        self.request_args = (method, path)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeConnection.response_status = 200
    FakeConnection.fail_request = False
    FakeConnection.latest = None
    monkeypatch.setattr(http.client, "HTTPConnection", FakeConnection)


def test_probe_calls_local_liveness_endpoint_and_closes_connection() -> None:
    assert container_healthcheck.is_live() is True
    assert container_healthcheck.main([]) == 0

    connection = FakeConnection.latest
    assert connection is not None
    assert (connection.host, connection.port, connection.timeout) == (
        "127.0.0.1",
        8000,
        5.0,
    )
    assert connection.request_args == ("GET", "/api/v1/live")
    assert connection.response.read_called is True
    assert connection.closed is True


def test_probe_fails_closed_for_non_success_status() -> None:
    FakeConnection.response_status = 503

    assert container_healthcheck.is_live() is False
    assert container_healthcheck.main([]) == 1


def test_probe_fails_closed_for_connection_error_and_still_closes() -> None:
    FakeConnection.fail_request = True

    assert container_healthcheck.is_live() is False
    assert container_healthcheck.main([]) == 1
    assert FakeConnection.latest is not None
    assert FakeConnection.latest.closed is True


def test_ready_mode_calls_readiness_endpoint() -> None:
    assert container_healthcheck.is_ready() is True
    assert container_healthcheck.main(["--ready"]) == 0

    connection = FakeConnection.latest
    assert connection is not None
    assert connection.request_args == ("GET", "/api/v1/ready")
    assert connection.closed is True


def test_unknown_probe_mode_fails_without_opening_connection() -> None:
    assert container_healthcheck.main(["--unknown"]) == 2
    assert FakeConnection.latest is None
