"""Traceback-free startup settings and policy validation launcher."""

from __future__ import annotations

import importlib
import sys


def main() -> int:
    """Validate configuration while rendering only stable redacted failures."""

    try:
        importlib.import_module("app.config")
    except Exception:
        print(
            "DocVault startup configuration rejected (CFG_SETTINGS_PARSE).",
            file=sys.stderr,
        )
        return 78

    bootstrap = importlib.import_module("app.bootstrap")
    return int(bootstrap.main())


if __name__ == "__main__":
    raise SystemExit(main())
