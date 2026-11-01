"""T-DATA-01: EDGAR client - declared User-Agent, <= 8 req/s, 429/403 backoff, cache, SSRF guard."""

from __future__ import annotations

import itertools
import random
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from app.core.clock import FakeClock
from app.modules.edgar.client import (
    EdgarClient,
    EdgarError,
    EdgarNotFoundError,
    EdgarUnavailableError,
    UrlRejectedError,
    archive_url,
    companyfacts_url,
    submissions_url,
    validate_user_agent,
)

UA = "StockPilot test@example.com"
URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


async def _public(_h: str) -> list[str]:
    return ["104.18.32.7"]


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    tmp_path: Path,
    clock: FakeClock | None = None,
    **kw: object,
) -> EdgarClient:
    return EdgarClient(
        UA,
        clock=clock or FakeClock(),
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        resolver=_public,
        rng=random.Random(1),
        **kw,  # type: ignore[arg-type]
    )


def test_user_agent_must_declare_contact() -> None:
    assert validate_user_agent('"StockPilot a@b.co"') == "StockPilot a@b.co"
    for bad in ("StockPilot <your-email>", "", "python-httpx", "StockPilot your-email@example.com"):
        with pytest.raises(ValueError, match="contact email"):
            validate_user_agent(bad)


def test_url_builders() -> None:
    assert companyfacts_url(320193).endswith("/CIK0000320193.json")
    assert submissions_url("1045810") == "https://data.sec.gov/submissions/CIK0001045810.json"
    assert (
        archive_url("320193", "0000320193-25-000079", "aapl-20250927.htm")
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )


async def test_user_agent_header_sent_and_body_returned(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["user-agent"])
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler, tmp_path) as c:
        res = await c.get(URL)
    assert seen == [UA]
    assert res.json() == {"ok": True}
    assert not res.from_cache
    assert len(res.sha256) == 64


async def test_never_more_than_8_requests_in_any_second(tmp_path: Path) -> None:
    clock = FakeClock()
    stamps: list[float] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        stamps.append(clock.monotonic())
        return httpx.Response(200, content=b"x")

    async with make_client(handler, tmp_path, clock) as c:
        for i in range(60):
            await c.get(f"{URL}?n={i}", max_age_s=0, force=True)
    assert len(stamps) == 60
    for i, t in enumerate(stamps):
        window = [s for s in stamps if t <= s < t + 1.0]
        assert len(window) <= 8, f"{len(window)} requests inside 1s starting at request {i}"
    assert stamps[-1] - stamps[0] >= 60 / 8 - 1.0  # it really waited rather than bursting


async def test_429_retry_after_is_honoured_then_succeeds(tmp_path: Path) -> None:
    clock = FakeClock()
    calls: list[float] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=b"data")

    async with make_client(handler, tmp_path, clock) as c:
        res = await c.get(URL)
    assert res.body == b"data"
    assert calls[1] - calls[0] >= 7.0


async def test_403_and_5xx_back_off_exponentially_with_jitter_then_give_up(tmp_path: Path) -> None:
    clock = FakeClock()
    calls: list[float] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic())
        return httpx.Response(403 if len(calls) % 2 else 503)

    async with make_client(handler, tmp_path, clock, max_retries=4, backoff_base_s=1.0) as c:
        with pytest.raises(EdgarUnavailableError):
            await c.get(URL)
    assert len(calls) == 5
    gaps = [b - a for a, b in itertools.pairwise(calls)]
    # equal jitter: delay in [0.5, 1.0] * base * 2^attempt
    for attempt, gap in enumerate(gaps):
        hi = 2.0**attempt
        assert 0.5 * hi - 1e-6 <= gap <= hi + 1.1, (attempt, gap)
    assert gaps[3] > gaps[0]  # grows


async def test_transport_errors_are_retried(tmp_path: Path) -> None:
    n = {"i": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] < 3:
            raise httpx.ConnectTimeout("boom")
        return httpx.Response(200, content=b"ok")

    async with make_client(handler, tmp_path) as c:
        assert (await c.get(URL)).body == b"ok"
    assert n["i"] == 3


async def test_404_is_not_retried_and_other_4xx_raise(tmp_path: Path) -> None:
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(404 if "missing" in str(req.url) else 400)

    async with make_client(handler, tmp_path) as c:
        with pytest.raises(EdgarNotFoundError):
            await c.get("https://data.sec.gov/missing.json")
        with pytest.raises(EdgarError):
            await c.get("https://data.sec.gov/bad.json")
    assert n["i"] == 2


async def test_disk_cache_hit_and_expiry(tmp_path: Path) -> None:
    clock = FakeClock()
    n = {"i": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(200, content=f"v{n['i']}".encode())

    async with make_client(handler, tmp_path, clock) as c:
        a = await c.get(URL, max_age_s=3600)
        b = await c.get(URL, max_age_s=3600)
        assert (a.body, b.body, b.from_cache) == (b"v1", b"v1", True)
        clock.advance(7200)
        d = await c.get(URL, max_age_s=3600)
        assert d.body == b"v2" and not d.from_cache
        e = await c.get(URL, max_age_s=None)  # any copy is fine
        assert e.from_cache
        f = await c.get(URL, force=True)
        assert f.body == b"v3"
    # a new client with the same cache dir reuses the files (resumable ingest)
    async with make_client(handler, tmp_path, clock) as c2:
        assert (await c2.get(URL)).from_cache


async def test_response_size_cap(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    async with make_client(handler, tmp_path, max_bytes=1000) as c:
        with pytest.raises(EdgarError, match="cap"):
            await c.get(URL)

    async def gen() -> AsyncIterator[bytes]:
        yield b"x" * 600
        yield b"y" * 600

    def chunked(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gen())

    async with make_client(chunked, tmp_path, max_bytes=1000) as c:
        with pytest.raises(EdgarError, match="cap"):
            await c.get(URL + "?s")


async def test_ssrf_urls_never_reach_the_network(tmp_path: Path) -> None:
    n = {"i": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(200)

    async with make_client(handler, tmp_path) as c:
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.169.254/",
            "https://evil.example.com/x",
            "https://localhost/x",
        ):
            with pytest.raises(UrlRejectedError):
                await c.get(url)
    assert n["i"] == 0


async def test_redirects_are_revalidated(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://data.sec.gov/final.json"})
        if req.url.path == "/evil":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
        if req.url.path == "/loop":
            return httpx.Response(302, headers={"Location": "/loop"})
        return httpx.Response(200, content=b"final")

    async with make_client(handler, tmp_path) as c:
        assert (await c.get("https://data.sec.gov/start")).body == b"final"
        with pytest.raises(UrlRejectedError):
            await c.get("https://data.sec.gov/evil")
        with pytest.raises(EdgarError, match="redirect"):
            await c.get("https://data.sec.gov/loop")


async def test_allowlisted_host_resolving_privately_is_blocked(tmp_path: Path) -> None:
    async def rebind(_h: str) -> list[str]:
        return ["169.254.169.254"]

    c = EdgarClient(
        UA,
        clock=FakeClock(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        cache_dir=tmp_path,
        resolver=rebind,
    )
    async with c:
        with pytest.raises(UrlRejectedError):
            await c.get(URL)
