"""T-DATA-05 (WS drop -> REST -> Finnhub -> cache, backoff + jitter) and T-DATA-06 (> 30 symbols)."""

from __future__ import annotations

import itertools
import logging
import random
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.clock import FakeClock
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.service import MarketDataService
from app.modules.marketdata.sources.base import UpstreamError
from app.modules.marketdata.store import StoredQuote
from app.modules.marketdata.subscriptions import SymbolDemand, plan_subscriptions
from app.modules.marketdata.supervisor import Backoff, FeedSupervisor, SupervisorTiming

START = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)  # Friday 11:00 ET


# ----------------------------------------------------------------------------------- T-DATA-06
def test_union_of_watchlists_and_holdings_prioritised_by_holders_then_watchers() -> None:
    plan = plan_subscriptions(
        [
            SymbolDemand("B", watchers=3),
            SymbolDemand("A", holders=1),
            SymbolDemand("C", watchers=1, holders=2),
            SymbolDemand("B", watchers=1),
        ]
    )
    assert plan.ws == ("C", "A", "B") and plan.rest == () and plan.total == 3 and not plan.overflow


def test_over_30_symbols_warns_and_overflows_to_rest(caplog: pytest.LogCaptureFixture) -> None:
    demand = [SymbolDemand(f"S{i:02d}", watchers=100 - i) for i in range(35)]
    demand.append(SymbolDemand("HELD", holders=1))
    with caplog.at_level(logging.WARNING, logger="app.marketdata.subscriptions"):
        plan = plan_subscriptions(demand, cap=30)
    assert len(plan.ws) == 30 and len(plan.rest) == 6 and plan.total == 36
    assert plan.ws[0] == "HELD"  # holdings outrank watchlist-only symbols
    assert "S34" in plan.rest and "S00" in plan.ws  # lowest-priority symbols are the REST ones
    warning = next(r for r in caplog.records if "exceeds websocket cap" in r.getMessage())
    assert warning.levelno == logging.WARNING and warning.total == 36  # type: ignore[attr-defined]
    assert set(plan.all) == {d.symbol for d in demand}  # nothing dropped


def test_warns_past_25_but_does_not_overflow(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.marketdata.subscriptions"):
        plan = plan_subscriptions([SymbolDemand(f"S{i:02d}") for i in range(27)], cap=30, warn_at=25)
    assert not plan.overflow
    assert any("approaching" in r.getMessage() for r in caplog.records)


def test_empty_demand_falls_back_to_the_universe() -> None:
    plan = plan_subscriptions([], fallback_universe=["AAPL", "MSFT"])
    assert plan.ws == ("AAPL", "MSFT")


# ----------------------------------------------------------------------------------- backoff
def test_backoff_grows_exponentially_with_jitter_and_caps() -> None:
    b = Backoff(1.0, 60.0, random.Random(1))
    delays = [b.next() for _ in range(10)]
    for i, d in enumerate(delays):
        ceiling = min(60.0, 2.0**i)
        assert ceiling / 2 <= d <= ceiling
    assert delays[-1] >= 30 and max(delays) <= 60
    assert len({round(d, 6) for d in delays[6:]}) > 1  # jittered, not constant
    b.reset()
    assert b.next() <= 1.0


def test_backoff_jitter_differs_between_clients() -> None:
    a, b = Backoff(1, 60, random.Random(1)), Backoff(1, 60, random.Random(2))
    assert [a.next() for _ in range(5)] != [b.next() for _ in range(5)]


# ----------------------------------------------------------------------------------- fakes
class FakeStream:
    name = "fake_ws"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.script: list[object] = []  # quotes to yield, then an Exception instance to raise
        self.calls: list[list[str]] = []

    async def stream(self, symbols: Sequence[str]) -> AsyncIterator[StoredQuote]:
        self.calls.append(list(symbols))
        for item in self.script:
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, StoredQuote)
            yield item


class FakePoll:
    def __init__(self, name: str, clock: FakeClock, kind: str = "polled") -> None:
        self.name = name
        self.clock = clock
        self.fail: Exception | None = None
        self.calls: list[list[str]] = []
        self.chunk: int | None = None

    async def fetch(self, symbols: Sequence[str]) -> dict[str, StoredQuote]:
        self.calls.append(list(symbols))
        if self.fail:
            raise self.fail
        now = self.clock.now()
        return {s: StoredQuote(s, Decimal("50"), now, now, self.name, "polled") for s in symbols}


def make(
    clock: FakeClock, *, ws: bool = True, with_fallback: bool = True
) -> tuple[FeedSupervisor, MarketDataService, FakeStream, FakePoll, FakePoll]:
    stream, rest, fh = FakeStream(clock), FakePoll("alpaca_rest_delayed", clock), FakePoll("finnhub", clock)
    holder: list[MarketDataService] = []
    sup = FeedSupervisor(
        clock=clock,
        on_quote=lambda q: holder[0].on_quote(q),
        plan=lambda: holder[0].current_plan(),
        stream=stream if ws else None,
        rest=rest,
        fallback=fh if with_fallback else None,
        timing=SupervisorTiming(backoff_base_s=1, backoff_cap_s=60, backoff_reset_after_s=30),
        rng=random.Random(5),
    )

    class State:
        market_override = None
        feed_override = None

    svc = MarketDataService(cfg=MarketDataConfig(autostart=False), clock=clock, state=State(), supervisor=sup)
    holder.append(svc)
    svc.set_plan_from_demand([SymbolDemand("AAPL", holders=1), SymbolDemand("MSFT", watchers=1)], [])
    return sup, svc, stream, rest, fh


def ws_quote(clock: FakeClock, symbol: str = "AAPL", price: str = "100") -> StoredQuote:
    return StoredQuote(symbol, Decimal(price), clock.now(), clock.now(), "alpaca_iex_ws", "stream")


# ----------------------------------------------------------------------------------- T-DATA-05
async def test_ws_quotes_flow_then_disconnect_fails_over_to_rest_then_finnhub_then_cache() -> None:
    clock = FakeClock(START)
    sup, svc, stream, rest, fh = make(clock)

    # 1. healthy WS session delivers a live quote, then the socket drops
    stream.script = [ws_quote(clock), UpstreamError("alpaca ws error 406: connection limit exceeded")]
    delay = await sup.ws_session_once()
    assert svc.store.get("AAPL") is not None and svc.quote_freshness("AAPL") == "live"
    assert not sup.ws_up and sup.poll_wakeup.is_set()  # REST poll triggered immediately
    assert 0.5 <= delay <= 1.0  # first backoff step

    # 2. REST polling takes over; badge says delayed / reconnecting, source is the REST label
    clock.advance(30)
    assert await sup.poll_once() == "rest"
    assert rest.calls[-1] == ["AAPL", "MSFT"]  # WS is down: REST covers everything
    q = svc.view(svc.store.get("MSFT"))  # type: ignore[arg-type]
    assert q.source == "alpaca_rest_delayed" and q.freshness == "delayed"
    assert str(svc.feed_state) == "reconnecting"

    # 3. REST fails -> Finnhub
    rest.fail = UpstreamError("alpaca rest rate limited (429)")
    clock.advance(30)
    assert await sup.poll_once() == "finnhub"
    assert svc.view(svc.store.get("AAPL")).source == "finnhub"  # type: ignore[arg-type]

    # 4. everything fails -> cache; quotes keep being served and age to stale
    fh.fail = UpstreamError("finnhub http 500")
    clock.advance(30)
    assert await sup.poll_once() == "cache"
    cached = svc.view(svc.store.get("AAPL"))  # type: ignore[arg-type]
    assert cached.source == "cache" and cached.freshness == "delayed"
    assert str(svc.feed_state) == "stale"
    clock.advance(minutes=16)
    assert await sup.poll_once() == "cache"
    assert svc.view(svc.store.get("AAPL")).freshness == "stale"  # type: ignore[arg-type]
    assert svc.fills_blocked("AAPL") and not svc.move_alerts_allowed("AAPL")


async def test_reconnect_resets_backoff_after_a_healthy_session_and_grows_on_flapping() -> None:
    clock = FakeClock(START)
    sup, _svc, stream, _rest, _fh = make(clock)
    delays: list[float] = []
    for _ in range(5):  # connection keeps failing straight away
        stream.script = [UpstreamError("down")]
        delays.append(await sup.ws_session_once())
    assert [round(d) for d in delays][-1] >= 8 and delays[0] <= 1.0 <= delays[-1]
    assert all(b >= a / 2 for a, b in itertools.pairwise(delays))

    class Slow(FakeStream):
        async def stream(self, symbols: Sequence[str]) -> AsyncIterator[StoredQuote]:
            yield ws_quote(clock)
            clock.advance(60)  # healthy for a minute, then drops
            raise UpstreamError("dropped")

    sup.stream = Slow(clock)
    delay = await sup.ws_session_once()
    assert sup.backoff.attempt == 1 and delay <= 1.0  # reset, then one step taken


async def test_ws_down_unexpected_exception_types_are_handled() -> None:
    clock = FakeClock(START)
    sup, _svc, stream, _rest, _fh = make(clock)
    stream.script = [ConnectionResetError("boom")]
    await sup.ws_session_once()
    assert not sup.ws_up and sup.last_error == "ConnectionResetError"


async def test_with_ws_healthy_only_overflow_symbols_use_rest() -> None:
    clock = FakeClock(START)
    sup, svc, _stream, rest, _fh = make(clock)
    svc.set_plan_from_demand([SymbolDemand(f"S{i:02d}", watchers=50 - i) for i in range(32)], [])
    svc.cfg = MarketDataConfig(autostart=False, ws_symbol_cap=30)
    plan = svc.plan
    assert len(plan.ws) == 30 and len(plan.rest) == 2
    sup.ws_up = True
    await sup.poll_once()
    assert rest.calls[-1] == list(plan.rest)  # T-DATA-06: lowest-priority symbols via REST
    assert svc.view(svc.store.get(plan.rest[0])).source == "alpaca_rest_delayed"  # type: ignore[arg-type]


async def test_finnhub_chunks_rotate_to_respect_the_rate_limit() -> None:
    clock = FakeClock(START)
    sup, svc, _stream, rest, fh = make(clock)
    svc.set_plan_from_demand([SymbolDemand(f"S{i}", watchers=10 - i) for i in range(7)], [])
    rest.fail = UpstreamError("down")
    fh.chunk = 3
    seen: list[str] = []
    for _ in range(3):
        await sup.poll_once()
        seen += fh.calls[-1]
    assert all(len(c) == 3 for c in fh.calls)
    assert len(set(seen)) == 7  # every symbol covered after a few polls


async def test_finnhub_only_mode_reports_delayed_feed() -> None:
    clock = FakeClock(START)
    sup, svc, _stream, _rest, fh = make(clock, ws=False)
    sup.rest, sup.fallback = fh, None
    assert await sup.poll_once() == "rest"  # Finnhub is the primary polled source in this mode
    assert svc.view(svc.store.get("AAPL")).source == "finnhub"  # type: ignore[arg-type]
    assert svc.feed_state == "delayed"
