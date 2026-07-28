"""Explicit LanceDB schema and maintenance entry point for the writer process."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run locked LanceDB maintenance.")
    parser.add_argument("operation", choices=("provision", "optimize"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    from app.services import lancedb_service
    from app.retrieval_store import RetrievalStoreError

    try:
        if args.operation == "provision":
            lancedb_service.provision()
            detail = "schema_ready"
        else:
            detail = lancedb_service.optimize().detail
    except RetrievalStoreError:
        print("LanceDB maintenance rejected (LANCEDB_MAINTENANCE_FAILED).")
        return 78
    print(f"LanceDB maintenance complete ({detail}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
