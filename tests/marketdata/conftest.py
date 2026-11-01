"""Fixtures for the market-data tests: an app factory with a market-data config override and a raw-ASGI SSE
client (httpx's ASGITransport buffers the whole body, so it cannot observe a stream)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.core.clock import Clock
from app.core.config import Settings
from app.main import create_app
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.service import MarketDataService

TEST_SYMBOLS = ["AAPL", "MSFT", "NVDA", "KO"]


@dataclass
class MdApp:
    app: Any
    client: httpx.AsyncClient

    @property
    def svc(self) -> MarketDataService:
        svc: MarketDataService = self.app.state.market_service
        return svc


@pytest.fixture
def md_app(test_database: str, settings: Settings) -> Callable[..., Any]:
    @asynccontextmanager
    async def _make(
        clock: Clock | None = None,
        cfg: MarketDataConfig | None = None,
        market_override: bool | None = None,
        **overrides: Any,
    ) -> AsyncIterator[MdApp]:
        app = create_app(settings.model_copy(update=overrides), clock=clock)
        base = cfg or MarketDataConfig(sim_tick_s=0.2, heartbeat_s=0.3, stream_symbols_refresh_s=0.5)
        app.state.marketdata_config = replace(base, autostart=base.autostart)
        app.state.market_override = market_override
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4242))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield MdApp(app, client)

    return _make


@pytest.fixture
def seed_symbols(ensure_tickers: Callable[..., Any]) -> Callable[[], Any]:
    async def _seed() -> None:
        await ensure_tickers(TEST_SYMBOLS)

    return _seed


@dataclass
class SseEvent:
    id: int | None
    event: str
    data: Any


def parse_sse(raw: bytes) -> list[SseEvent]:
    events: list[SseEvent] = []
    for block in raw.decode().split("\n\n"):
        if not block.strip() or block.startswith("retry:"):
            continue
        ev_id: int | None = None
        name = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("id: "):
                ev_id = int(line[4:])
            elif line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        events.append(SseEvent(ev_id, name, json.loads(data) if data else None))
    return events


class SseSession:
    """Drives the ASGI app directly so chunks are observable as they are produced."""

    def __init__(
        self, app: Any, token: str | None, headers: dict[str, str] | None = None, extra_qs: str = ""
    ) -> None:
        qs = (f"token={token}" if token is not None else "") + extra_qs
        self._scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/stream",
            "raw_path": b"/v1/stream",
            "query_string": qs.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("203.0.113.9", 4242),
            "server": ("test", 80),
        }
        self._app = app
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sent_request = False
        self._task: asyncio.Task[None] | None = None
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._buffer = b""
        self.events: list[SseEvent] = []

    async def _receive(self) -> dict[str, Any]:
        if not self._sent_request:
            self._sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await self._inbox.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._outbox.put(message)

    async def open(self) -> SseSession:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        start = await asyncio.wait_for(self._outbox.get(), 5)
        assert start["type"] == "http.response.start"
        self.status = start["status"]
        self.headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return self

    async def next_events(self, *, timeout: float = 3.0, want: int = 1) -> list[SseEvent]:
        """Block until at least `want` more complete events arrived (or time runs out)."""
        got: list[SseEvent] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(got) < want:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self._outbox.get(), remaining)
            except TimeoutError:
                break
            if msg["type"] == "http.response.body":
                self._buffer += msg.get("body", b"")
                *complete, self._buffer = self._buffer.split(b"\n\n")
                for block in complete:
                    parsed = parse_sse(block + b"\n\n")
                    got.extend(parsed)
                if not msg.get("more_body", False):
                    break
        self.events.extend(got)
        return got

    async def until(self, predicate: Callable[[SseEvent], bool], *, timeout: float = 3.0) -> SseEvent | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for ev in await self.next_events(timeout=deadline - loop.time(), want=1):
                if predicate(ev):
                    return ev
        return None

    async def close(self) -> None:
        await self._inbox.put({"type": "http.disconnect"})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()


async def mint_stream_token(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post("/v1/stream/token", headers=headers)
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["token"]
    return token


def uuid_str(u: UUID) -> str:
    return str(u)
