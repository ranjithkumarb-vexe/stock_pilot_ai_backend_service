"""Load smoke for the dashboard endpoints (BE-22; NFR-01 / T-PERF-01 first cut). Stand-in for k6 / `hey`, which are not installed.

    # server in dev auth mode (personas must be seeded), rate limits off so latency rather than throttling is measured:
    AUTH_MODE=dev RATE_LIMIT_ENABLED=false uv run uvicorn app.main:create_app --factory --port 8009 --no-access-log
    uv run python -m scripts.load_smoke --base-url http://127.0.0.1:8009 --users 50 --loads 6

Each virtual user signs in as a seeded persona and repeatedly "loads the dashboard": the read endpoints the SPA fires on page
load, at most `--connections` (a browser's ~6) in flight at once, then thinks for `--think-s`. The page-load time is the
slowest of those requests. Prints per-endpoint and per-page-load p50/p95/p99 and every distinct error; exit code 1 when the
page-load p95 exceeds --budget-ms (default 1500, SPEC NFR-01) or any request failed. One process on one machine against a
local server: it measures the server under concurrency, not the network, Render or Supabase latency.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

PERSONAS = ("priya", "alex", "sam")
DASHBOARD = (
    "/v1/me",
    "/v1/portfolio",
    "/v1/portfolio/risk",
    "/v1/portfolio/performance",
    "/v1/watchlist",
    "/v1/feed",
    "/v1/alerts",
    "/v1/proposals",
    "/v1/market/status",
    "/v1/orders",
)


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


@dataclass
class Result:
    per_endpoint: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    page_loads: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wall_s: float = 0.0


async def login(client: httpx.AsyncClient, persona: str) -> dict[str, str]:
    r = await client.post("/v1/auth/dev-login", json={"persona": persona})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def timed(
    client: httpx.AsyncClient, gate: asyncio.Semaphore, path: str, headers: dict[str, str]
) -> tuple[str, float, int | str]:
    async with gate:  # a browser keeps ~6 connections to one host: model that, not ten simultaneous requests
        t0 = time.perf_counter()
        status: int | str
        try:
            status = (await client.get(path, headers=headers)).status_code
        except httpx.HTTPError as exc:
            status = type(exc).__name__
        return path, (time.perf_counter() - t0) * 1000.0, status


async def virtual_user(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    loads: int,
    think_s: float,
    connections: int,
    out: Result,
) -> None:
    gate = asyncio.Semaphore(connections)
    for _ in range(loads):
        t0 = time.perf_counter()
        results = await asyncio.gather(*(timed(client, gate, p, headers) for p in DASHBOARD))
        out.page_loads.append((time.perf_counter() - t0) * 1000.0)
        for path, ms, status in results:
            out.per_endpoint[path].append(ms)
            if status != 200:
                out.errors.append(f"{path} -> {status}")
        await asyncio.sleep(think_s)


async def run(base_url: str, users: int, loads: int, think_s: float, connections: int) -> Result:
    out = Result()
    limits = httpx.Limits(max_connections=users * connections, max_keepalive_connections=users * connections)
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, limits=limits) as client:
        tokens = {p: await login(client, p) for p in PERSONAS}
        t0 = time.perf_counter()
        await asyncio.gather(
            *(
                virtual_user(client, tokens[PERSONAS[i % len(PERSONAS)]], loads, think_s, connections, out)
                for i in range(users)
            )
        )
        out.wall_s = time.perf_counter() - t0
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dashboard load smoke")
    ap.add_argument("--base-url", default="http://127.0.0.1:8009")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--loads", type=int, default=6, help="dashboard loads per virtual user")
    ap.add_argument("--think-s", type=float, default=1.0, help="pause between one user's dashboard loads")
    ap.add_argument("--connections", type=int, default=6, help="parallel requests per user (browser limit)")
    ap.add_argument("--warmup", type=int, default=1, help="dashboard loads per persona before measuring")
    ap.add_argument("--budget-ms", type=float, default=1500.0, help="p95 page-load budget (SPEC NFR-01)")
    args = ap.parse_args(argv)
    if args.warmup:
        asyncio.run(run(args.base_url, len(PERSONAS), args.warmup, 0.0, args.connections))
    res = asyncio.run(run(args.base_url, args.users, args.loads, args.think_s, args.connections))
    n_req = sum(len(v) for v in res.per_endpoint.values())
    print(
        f"{args.users} virtual users (think {args.think_s}s, {args.connections} connections each) x {args.loads} "
        f"dashboard loads = {len(res.page_loads)} loads, {n_req} requests in {res.wall_s:.1f}s "
        f"({n_req / res.wall_s:.0f} req/s)"
    )
    print(f"{'endpoint':32} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}   (ms)")
    for path in DASHBOARD:
        v = res.per_endpoint[path]
        print(f"{path:32} {statistics.median(v):8.0f} {pct(v, 0.95):8.0f} {pct(v, 0.99):8.0f} {max(v):8.0f}")
    p95 = pct(res.page_loads, 0.95)
    print(
        f"{'PAGE LOAD (slowest of ten)':32} {statistics.median(res.page_loads):8.0f} {p95:8.0f} "
        f"{pct(res.page_loads, 0.99):8.0f} {max(res.page_loads):8.0f}"
    )
    distinct = sorted(set(res.errors))
    print(f"errors: {len(res.errors)}" + (f"  distinct: {distinct[:6]}" if res.errors else ""))
    ok = not res.errors and p95 <= args.budget_ms
    print(
        f"dashboard page-load p95 {p95:.0f} ms vs budget {args.budget_ms:.0f} ms: {'OK' if ok else 'FAILED'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
