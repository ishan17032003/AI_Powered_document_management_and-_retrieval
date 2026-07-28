"""Small repeatable HTTP load harness for PERF-002/003.

It reports measurements only; it never changes deployment limits automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx


async def run(base_url: str, *, requests: int, concurrency: int) -> dict[str, object]:
    if not 1 <= requests <= 100_000 or not 1 <= concurrency <= 256:
        raise ValueError("load bounds are invalid")
    semaphore = asyncio.Semaphore(concurrency)
    timings: list[float] = []
    statuses: list[int] = []

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        async def one() -> None:
            async with semaphore:
                started = time.perf_counter()
                response = await client.get("/api/v1/live")
                timings.append((time.perf_counter() - started) * 1000)
                statuses.append(response.status_code)

        await asyncio.gather(*(one() for _ in range(requests)))
    ordered = sorted(timings)
    percentile = lambda fraction: ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]
    return {
        "requests": requests,
        "concurrency": concurrency,
        "errors": sum(status >= 500 for status in statuses),
        "rejections": sum(status in {429, 503} for status in statuses),
        "p50_ms": round(percentile(0.50), 3),
        "p95_ms": round(percentile(0.95), 3),
        "p99_ms": round(percentile(0.99), 3),
        "mean_ms": round(statistics.fmean(timings), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.base_url, requests=args.requests, concurrency=args.concurrency)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
