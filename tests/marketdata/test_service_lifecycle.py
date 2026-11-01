"""MarketDataService lifecycle with a (fake) real-source supervisor: on-demand REST, background tasks, shutdown."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.clock import FakeClock
from app.core.db import Database, set_database
from app.core.pricesource import QuoteUnavailableError
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.service import MarketDataService
from app.modules.marketdata.sources.base import UpstreamError
from app.modules.marketdata.store import StoredQuote
from app.modules.marketdata.subscriptions import SymbolDemand
from app.modules.marketdata.supervisor import FeedSupervisor, SupervisorTiming

START = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


class State:
    market_override = None
    feed_override = None


class Poll:
    name = "poll"

    def __init__(self, clock: FakeClock, fail: bool = False) -> None:
        self.clock, self.fail, self.calls = clock, fail, 0

    async def fetch(self, symbols: Sequence[str]) -> dict[str, StoredQuote]:
        self.calls += 1
        if self.fail:
            raise UpstreamError("down")
        n = self.clock.now()
        return {s: StoredQuote(s, Decimal("10"), n, n, "alpaca_rest_delayed", "polled") for s in symbols}


class Stream:
    name = "ws"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    async def stream(self, symbols: Sequence[str]) -> AsyncIterator[StoredQuote]:
        n = self.clock.now()
        for s in symbols:
            yield StoredQuote(s, Decimal("11"), n, n, "alpaca_iex_ws", "stream")
        await asyncio.sleep(3600)


def build(
    clock: FakeClock, rest: Poll | None, fallback: Poll | None, stream: Stream | None = None
) -> MarketDataService:
    holder: list[MarketDataService] = []
    sup = FeedSupervisor(
        clock=clock,
        on_quote=lambda q: holder[0].on_quote(q),
        plan=lambda: holder[0].current_plan(),
        stream=stream,
        rest=rest,
        fallback=fallback,
        timing=SupervisorTiming(poll_interval_s=0.05),
        rng=random.Random(1),
    )
    svc = MarketDataService(cfg=MarketDataConfig(autostart=True), clock=clock, state=State(), supervisor=sup)
    holder.append(svc)
    return svc


async def test_on_demand_quote_uses_rest_then_fallback_and_reports_unavailable() -> None:
    clock = FakeClock(START)
    rest, fb = Poll(clock, fail=True), Poll(clock)
    svc = build(clock, rest, fb)
    q = await svc.get_quote("AAPL")
    assert q is not None and q.source == "alpaca_rest_delayed" and q.freshness == "delayed"
    assert rest.calls == 1 and fb.calls == 1 and "AAPL" in svc.symbols()  # later polls include it
    assert svc.store.get("AAPL") is not None
    fb.fail = True
    assert await svc.get_quote("MSFT") is None
    with pytest.raises(QuoteUnavailableError):
        await svc.price_source.get_last("MSFT")
    none = build(clock, None, None)
    assert await none.get_quote("X") is None


async def test_start_runs_ws_and_poll_tasks_and_stop_cancels_and_closes(settings: object) -> None:
    clock = FakeClock(START)
    svc = build(clock, Poll(clock), None, Stream(clock))
    closed: list[bool] = []

    async def closer() -> None:
        closed.append(True)

    async def bad_closer() -> None:
        raise RuntimeError("x")

    svc.closers.extend([bad_closer, closer])
    svc.set_plan_from_demand([SymbolDemand("AAPL", holders=1), SymbolDemand("MSFT", watchers=1)], [])
    db = Database(settings)  # type: ignore[arg-type]
    await db.open()
    set_database(db)
    try:
        await svc.start()
        assert len(svc._tasks) == 3  # plan loop + ws + poll
        for _ in range(60):
            await asyncio.sleep(0.05)
            if svc.supervisor and svc.supervisor.ws_up and svc.store.get("AAPL"):
                break
        assert svc.supervisor is not None and svc.supervisor.ws_up
        assert svc.store.get("AAPL") is not None and svc.store.get("AAPL").source == "alpaca_iex_ws"  # type: ignore[union-attr]
        await svc.stop()
    finally:
        set_database(None)
        await db.close()
    assert closed == [True] and svc._tasks == []


async def test_plan_loop_picks_up_new_symbols_for_the_simulator(
    settings: object, ensure_tickers: object
) -> None:
    from app.modules.marketdata.sources.simulated import SimulatedSource

    clock = FakeClock(START)
    sim = SimulatedSource({}, clock, seed=1)
    svc = MarketDataService(
        cfg=MarketDataConfig(autostart=True, subscription_refresh_s=0.05),
        clock=clock,
        state=State(),
        simulated=sim,
    )
    db = Database(settings)  # type: ignore[arg-type]
    await db.open()
    set_database(db)
    try:
        await svc.start()
        svc._extra.add("ZZTEST")
        await asyncio.sleep(0.2)
        await svc.stop()
    finally:
        set_database(None)
        await db.close()
    assert svc.symbols()  # universe from the DB was loaded by the refresh


async def test_refresh_and_anchor_failures_are_contained() -> None:
    clock = FakeClock(START)
    svc = MarketDataService(cfg=MarketDataConfig(autostart=False), clock=clock, state=State())
    set_database(None)
    plan = await svc.refresh_plan()  # no database: RuntimeError is swallowed
    assert plan.total == 0
    await svc.load_anchors()  # simulated is None and supervisor is None: no-op
    svc.simulated = None
    svc.seed_store()  # no-op without a simulator
    assert await svc.tick_once() == 0
