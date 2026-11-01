"""SimulatedSource: seeded determinism, anchoring, labelling; synthetic bars; tick loop gating."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.clock import FakeClock
from app.core.pricesource import PriceSource, Quote, QuoteUnavailableError
from app.modules.marketdata.bars import synth_bars_5m
from app.modules.marketdata.calendar import NyseCalendar
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.service import MarketDataService
from app.modules.marketdata.sources.simulated import DEFAULT_ANCHORS, SimulatedSource, fallback_anchor

OPEN_TS = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)  # Friday 11:00 ET


def _src(
    seed: int = 42, clock: FakeClock | None = None, anchors: dict[str, Decimal] | None = None
) -> SimulatedSource:
    return SimulatedSource(anchors or {"AAPL": Decimal("200.00")}, clock or FakeClock(OPEN_TS), seed=seed)


def _walk(src: SimulatedSource, n: int, symbol: str = "AAPL") -> list[Decimal]:
    return [src.step([symbol])[0].price for _ in range(n)]


def test_same_seed_same_walk_different_seed_differs() -> None:
    a, b, c = _walk(_src(1), 200), _walk(_src(1), 200), _walk(_src(2), 200)
    assert a == b
    assert a != c


def test_symbols_have_independent_streams() -> None:
    src = _src(anchors={"AAPL": Decimal("200"), "MSFT": Decimal("200")})
    assert _walk(src, 50, "AAPL") != _walk(src, 50, "MSFT")


def test_walk_stays_anchored_and_is_labelled_simulated() -> None:
    src = _src()
    prices = _walk(src, 20_000)
    assert min(prices) > Decimal("200") * Decimal("0.9") and max(prices) < Decimal("200") * Decimal("1.1")
    assert len(set(prices)) > 100  # it actually moves
    q = src.step(["AAPL"])[0]
    assert q.source == "simulated"
    assert q.prev_close == Decimal("200.00")  # anchor = last known close
    assert q.bid is not None and q.ask is not None and q.bid <= q.price <= q.ask


def test_anchor_resolution_order() -> None:
    src = SimulatedSource({"AAPL": Decimal("123.45")}, FakeClock(), seed=1)
    assert src.anchor("AAPL") == Decimal("123.45")  # DB close wins
    assert src.anchor("NVDA") == DEFAULT_ANCHORS["NVDA"]  # design value
    assert src.anchor("ZZZZ") == fallback_anchor("ZZZZ") == fallback_anchor("ZZZZ")  # deterministic
    src.set_anchor("AAPL", Decimal("10"))
    assert src.anchor("AAPL") == Decimal("10")


async def test_get_last_is_a_price_source() -> None:
    src = _src()
    assert isinstance(src, PriceSource)
    q = await src.get_last("AAPL")
    assert q.source == "simulated" and q.price > 0
    with pytest.raises(QuoteUnavailableError):
        await src.get_last("")


async def test_subscribe_streams_ticks() -> None:
    src = _src()
    src.tick_s = 0.01
    got = []
    async for q in src.subscribe(["AAPL"]):
        got.append(q.price)
        if len(got) == 5:
            break
    assert len(got) == 5


def test_synthetic_bars_are_deterministic_and_end_at_the_live_price() -> None:
    cal = NyseCalendar()
    kw = {"symbol": "AAPL", "calendar": cal, "now": OPEN_TS, "anchor": 200.0, "last_price": 203.21, "seed": 7}
    a = synth_bars_5m(**kw)  # type: ignore[arg-type]
    b = synth_bars_5m(**kw)  # type: ignore[arg-type]
    assert a == b and 0 < len(a) <= 78
    assert a[-1].close == 203.21  # bridged to the current simulated price
    assert all(x.low <= min(x.open, x.close) and x.high >= max(x.open, x.close) for x in a)
    assert all(a[i].time < a[i + 1].time for i in range(len(a) - 1))
    # in-progress session: bars run from the open to now (11:00 ET -> 90 min = 18 bars)
    assert len(synth_bars_5m(**{**kw, "limit": 500})) >= 18  # type: ignore[arg-type]
    other = synth_bars_5m(**{**kw, "seed": 8})  # type: ignore[arg-type]
    assert [x.close for x in other] != [x.close for x in a]


def test_synthetic_bars_span_previous_sessions_when_limit_is_large() -> None:
    bars = synth_bars_5m(
        symbol="MSFT", calendar=NyseCalendar(), now=OPEN_TS, anchor=500.0, last_price=None, limit=200
    )
    assert len(bars) == 200
    days = {b.time.date() for b in bars}
    assert len(days) >= 3


async def test_tick_once_is_gated_by_session_and_feed_state() -> None:
    class State:
        market_override: bool | None = None
        feed_override: str | None = None

    clock = FakeClock(datetime(2026, 11, 1, 15, 0, tzinfo=UTC))  # Sunday
    state = State()
    sim = SimulatedSource({"AAPL": Decimal("200")}, clock, seed=3)
    svc = MarketDataService(cfg=MarketDataConfig(autostart=False), clock=clock, state=state, simulated=sim)
    svc.set_plan_from_demand([], ["AAPL"])
    seen: list[Quote] = []
    svc.add_listener(seen.append)
    assert await svc.tick_once() == 0  # closed: nothing moves
    state.market_override = True
    assert await svc.tick_once() == 1 and seen and seen[-1].freshness == "live"
    state.feed_override = "stale"
    assert await svc.tick_once() == 0  # stale feed: ticks stop, so quotes really age
    state.feed_override = "reconnecting"
    assert await svc.tick_once() == 0
    state.feed_override = "delayed"
    produced = sum([await svc.tick_once() for _ in range(10)])
    assert produced == 2  # every 5th interval
    assert str(seen[-1].freshness) == "delayed"
    await asyncio.sleep(0)
