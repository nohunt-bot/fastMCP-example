#!/usr/bin/env python3
"""Load-test the skill server over HTTP.

    uv run skill-mcp --port 8000                    # terminal 1
    uv run python bench.py --concurrency 32         # terminal 2

Each worker holds one persistent MCP session and issues calls back-to-back, so
the numbers reflect steady-state server cost, not connection setup. Reported
latency is end-to-end client-observed, which is the number that matters — it
includes JSON-RPC framing, pydantic validation and HTTP on both sides.

Scenarios:
  catalog  list_skills           the hot path: pure in-memory, no I/O
  load     load_skill            cached body read
  search   list_skills(query=)   scored scan over the index
  script   run_skill_script      subprocess spawn, bounded by --max-script-concurrency
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any, Awaitable, Callable

from fastmcp import Client

SCENARIOS: dict[str, tuple[str, dict[str, Any]]] = {
    "catalog": ("list_skills", {}),
    "search": ("list_skills", {"query": "csv data"}),
    "load": ("load_skill", {"name": "csv-profile"}),
    "section": ("load_skill", {"name": "csv-profile", "section": "Usage"}),
    "script": (
        "run_skill_script",
        {
            "name": "text-stats",
            "script": "scripts/wordcount.py",
            "stdin": "the quick brown fox jumps over the lazy dog. again and again.",
        },
    ),
}


async def _worker(
    url: str, tool: str, payload: dict[str, Any], iterations: int, latencies: list[float]
) -> int:
    errors = 0
    async with Client(url) as client:
        for _ in range(iterations):
            started = time.perf_counter()
            try:
                await client.call_tool(tool, payload)
            except Exception:
                errors += 1
            latencies.append((time.perf_counter() - started) * 1000)
    return errors


def _percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


async def run_scenario(
    url: str, name: str, concurrency: int, iterations: int, warmup: int
) -> dict[str, Any]:
    tool, payload = SCENARIOS[name]

    if warmup:
        async with Client(url) as client:
            for _ in range(warmup):
                await client.call_tool(tool, payload)

    latencies: list[float] = []
    started = time.perf_counter()
    errors = sum(
        await asyncio.gather(
            *(_worker(url, tool, payload, iterations, latencies) for _ in range(concurrency))
        )
    )
    elapsed = time.perf_counter() - started

    ordered = sorted(latencies)
    return {
        "scenario": name,
        "tool": tool,
        "calls": len(latencies),
        "errors": errors,
        "seconds": round(elapsed, 3),
        "rps": round(len(latencies) / elapsed, 1) if elapsed else 0.0,
        "mean_ms": round(statistics.fmean(ordered), 3) if ordered else 0.0,
        "p50_ms": round(_percentile(ordered, 0.50), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "p99_ms": round(_percentile(ordered, 0.99), 3),
    }


async def main_async(args: argparse.Namespace) -> None:
    names = args.scenario or list(SCENARIOS)
    print(f"target={args.url}  concurrency={args.concurrency}  iterations={args.iterations}\n")
    header = f"{'scenario':<10}{'calls':>8}{'err':>5}{'rps':>12}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}"
    print(header)
    print("-" * len(header))
    for name in names:
        row = await run_scenario(args.url, name, args.concurrency, args.iterations, args.warmup)
        print(
            f"{row['scenario']:<10}{row['calls']:>8}{row['errors']:>5}"
            f"{row['rps']:>12,.1f}{row['p50_ms']:>10.2f}{row['p95_ms']:>10.2f}{row['p99_ms']:>10.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--iterations", type=int, default=50, help="calls per worker")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument(
        "--scenario", action="append", choices=list(SCENARIOS), help="repeatable; default all"
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
