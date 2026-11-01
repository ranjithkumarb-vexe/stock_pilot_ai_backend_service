"""T-DATA-02: stale quotes (> 15 min in regular hours) -> Stale badge, no move alerts, no market fills."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.clock import FakeClock
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.freshness import (
    allows_move_alerts,
    blocks_market_fills,
    compute_freshness,
)
from app.modules.marketdata.service import MarketDataService
from app.modules.marketdata.store import StoredQuote


class _State:
    market_override: bool | None = None
    feed_override: str | None = None


@pytest.mark.parametrize(
    ("kind", "age", "expected"),
    [
        ("stream", 0.0, "live"),
        ("stream", 4.9, "live"),
        ("stream", 5.0, "delayed"),
        ("stream", 600.0, "delayed"),
        ("stream", 900.0, "delayed"),
        ("stream", 900.1, "stale"),
        ("polled", 0.0, "delayed"),
        ("polled", 899.0, "delayed"),
        ("polled", 901.0, "stale"),
    ],
)
def test_regular_session_matrix(kind: str, age: float, expected: str) -> None:
    assert compute_freshness(session="regular", kind=kind, age_s=age) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("session", ["pre", "post", "closed"])
def test_outside_regular_session_is_closed_even_when_old_or_overridden(session: str) -> None:
    assert compute_freshness(session=session, kind="stream", age_s=99999) == "closed"  # type: ignore[arg-type]
    assert compute_freshness(session=session, kind="stream", age_s=1, feed_state="stale") == "closed"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("feed", "expected"),
    [("live", "live"), ("delayed", "delayed"), ("reconnecting", "delayed"), ("stale", "stale")],
)
def test_feed_override_wins_inside_regular_session(feed: str, expected: str) -> None:
    got = compute_freshness(session="regular", kind="stream", age_s=0, feed_state=feed)  # type: ignore[arg-type]
    assert got == expected


def test_policy_helpers() -> None:
    assert blocks_market_fills("stale") and not blocks_market_fills("delayed")
    assert allows_move_alerts("live") and allows_move_alerts("delayed")
    assert not allows_move_alerts("stale") and not allows_move_alerts("closed")


def _service(clock: FakeClock, state: _State | None = None) -> MarketDataService:
    return MarketDataService(cfg=MarketDataConfig(autostart=False), clock=clock, state=state or _State())


def _quote(clock: FakeClock, kind: str = "stream") -> StoredQuote:
    now = clock.now()
    return StoredQuote("AAPL", Decimal("100"), now, now, "alpaca_iex_ws", kind)  # type: ignore[arg-type]


def test_quote_goes_live_delayed_stale_with_the_clock() -> None:
    clock = FakeClock(datetime(2026, 10, 30, 15, 0, tzinfo=UTC))  # Friday 11:00 ET, regular
    svc = _service(clock)
    svc.on_quote(_quote(clock))
    assert svc.view(svc.store.get("AAPL")).freshness == "live"  # type: ignore[arg-type]
    assert svc.quote_freshness("AAPL") == "live"
    clock.advance(10)
    assert svc.quote_freshness("AAPL") == "delayed"
    clock.advance(minutes=14, seconds=50)  # 15 min exactly is still delayed
    assert svc.quote_freshness("AAPL") == "delayed"
    clock.advance(20)
    assert svc.quote_freshness("AAPL") == "stale"
    assert svc.fills_blocked("AAPL")  # market-order fills blocked
    assert not svc.move_alerts_allowed("AAPL")  # unusual-move alerts suppressed


def test_unknown_symbol_blocks_fills_and_alerts() -> None:
    svc = _service(FakeClock())
    assert svc.fills_blocked("ZZZ") and not svc.move_alerts_allowed("ZZZ")


def test_outside_session_old_quote_is_closed_not_stale() -> None:
    clock = FakeClock(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))  # Thanksgiving: no session, no false stale
    svc = _service(clock)
    svc.on_quote(
        StoredQuote(
            "AAPL", Decimal("1"), clock.now() - timedelta(days=1), clock.now() - timedelta(days=1), "x"
        )
    )
    assert svc.quote_freshness("AAPL") == "closed"
    assert not svc.fills_blocked("AAPL")  # closed != stale (closed is handled by the queue-to-open rule)
    assert svc.view(svc.store.get("AAPL")).session == "closed"  # type: ignore[arg-type]


def test_overrides_are_read_from_app_state() -> None:
    clock = FakeClock(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))  # holiday
    state = _State()
    svc = _service(clock, state)
    svc.on_quote(_quote(clock))
    assert svc.session_now() == "closed"
    state.market_override = True  # what stream A's engine / dev endpoint toggles
    assert svc.session_now() == "regular" and svc.market_open()
    state.feed_override = "stale"
    assert svc.quote_freshness("AAPL") == "stale"
    state.feed_override = "reconnecting"
    assert svc.quote_freshness("AAPL") == "delayed"
    state.market_override = False
    assert svc.quote_freshness("AAPL") == "closed"
