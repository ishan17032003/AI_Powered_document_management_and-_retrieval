"""Emit a bounded D-08 workload/hardware profile for performance runs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path


def profile(*, concurrency: int = 8, duration_seconds: int = 60) -> dict[str, object]:
    if not 1 <= concurrency <= 256 or not 1 <= duration_seconds <= 86_400:
        raise ValueError("profile bounds are invalid")
    disk = shutil.disk_usage(Path.cwd())
    return {
        "schema": "docvault.d08-workload-profile.v1",
        "captured_at_epoch": int(time.time()),
        "host": {"platform": os.uname().sysname, "machine": os.uname().machine, "logical_cpus": os.cpu_count() or 1},
        "memory_vram": {"gpu_vram_bytes": None, "measurement": "operator_required"},
        "workload": {"concurrency": concurrency, "duration_seconds": duration_seconds, "lanes": ["auth", "metadata", "text_search", "visual_validation", "upload", "audit"]},
        "corpus": {"documents": 44, "versions": 44, "recorded_pages": 202, "source_bytes": 41_244_073},
        "disk": {"total_bytes": disk.total, "free_bytes": disk.free},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration-seconds", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(profile(concurrency=args.concurrency, duration_seconds=args.duration_seconds), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
