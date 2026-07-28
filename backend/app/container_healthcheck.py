"""Dependency-free liveness probe for the backend container image."""

from __future__ import annotations

import http.client
import sys
from collections.abc import Sequence

_HOST = "127.0.0.1"
_PORT = 8000
_LIVE_PATH = "/api/v1/live"
_READY_PATH = "/api/v1/ready"
_TIMEOUT_SECONDS = 5.0


def _probe(path: str) -> bool:
    """Return whether the local web process successfully serves ``path``."""

    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(
            _HOST,
            _PORT,
            timeout=_TIMEOUT_SECONDS,
        )
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def is_live() -> bool:
    """Check process liveness without testing external dependencies."""

    return _probe(_LIVE_PATH)


def is_ready() -> bool:
    """Check that required local dependencies are ready to serve traffic."""

    return _probe(_READY_PATH)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        healthy = is_live()
    elif arguments == ["--ready"]:
        healthy = is_ready()
    else:
        return 2
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
