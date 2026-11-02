"""Service wiring against the real test DB (anchors from prices_daily, subscription set from watchlists and
holdings via the SECURITY DEFINER function) and source selection by `MARKET_DATA_SOURCE`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.core.clock import FakeClock
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.factory import build_service
from app.modules.marketdata.sources.alpaca import AlpacaRest, AlpacaStream
from app.modules.marketdata.sources.finnhub import FinnhubRest
from tests.marketdata.test_sse import give

pytestmark = pytest.mark.db


class _State:
    market_override = None
    feed_override = None


async def test_anchors_and_prev_close_come_from_prices_daily(
    md_app: Callable[..., Any],
    seed_symbols: Callable[[], Any],
    db_admin: Any,
    make_user: Any,
    auth_headers: Any,
) -> None:
    await seed_symbols()
    await db_admin.execute("delete from prices_daily where symbol = 'NVDA'")
    await db_admin.execute(
        "insert into prices_daily (symbol, date, close) values ('NVDA', $1, 111.11), ('NVDA', $2, 122.22)",
        date(2026, 10, 28),
        date(2026, 10, 29),
    )
    uid = await make_user()
    from datetime import UTC, datetime

    async with md_app(clock=FakeClock(datetime(2026, 10, 30, 15, 0, tzinfo=UTC))) as m:
        q = (await m.client.get("/v1/quotes?symbols=NVDA", headers=auth_headers(uid))).json()["items"][0]
        assert q["prev_close"] == 122.22  # last close before "today"
        assert (
            abs(q["price"] / 122.22 - 1) < 0.1
        )  # simulator is anchored to it, not to the design default 182.40
    await db_admin.execute("delete from prices_daily where symbol = 'NVDA'")


async def test_subscription_plan_is_the_union_of_watchlists_and_holdings(
    md_app: Callable[..., Any],
    ensure_tickers: Callable[..., Any],
    db_admin: Any,
    make_user: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    syms = [f"Z{i:02d}" for i in range(34)]
    await ensure_tickers(syms)
    a, b = await make_user(), await make_user()
    await give(db_admin, a, syms[:20], hold=[syms[33]])
    await give(db_admin, b, syms[10:32])  # Z32 is nobody's: not subscribed
    wanted = {*syms} - {syms[32]}
    async with md_app() as m:
        # the simulator has no vendor cap: everything is served, nothing dropped
        assert wanted <= set(m.svc.symbols())  # (the whole universe is served too, hence Z32 is present)
        # a capped vendor: 30 on the WS, the rest on REST, with a warning (T-DATA-06 against real data)
        cfg = MarketDataConfig(source="alpaca", alpaca_key_id="k", alpaca_secret="s", autostart=False)
        capped = build_service(cfg, FakeClock(), _State())
        with caplog.at_level(logging.WARNING, logger="app.marketdata.subscriptions"):
            plan = await capped.refresh_plan()
    ours = [s for s in plan.all if s in syms]
    assert set(ours) == wanted and len(ours) == 33
    assert len(plan.ws) == 30 and plan.overflow
    assert plan.ws[0] == syms[33]  # the held symbol outranks watchlist-only ones
    assert any("exceeds websocket cap" in r.getMessage() for r in caplog.records)


def test_factory_selects_source_and_falls_back_to_simulator_without_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    with caplog.at_level(logging.WARNING, logger="app.marketdata.factory"):
        assert build_service(MarketDataConfig(source="alpaca"), clock, _State()).is_simulated
        assert build_service(MarketDataConfig(source="finnhub"), clock, _State()).is_simulated
    assert sum("using the simulator" in r.getMessage() for r in caplog.records) == 2
    assert build_service(MarketDataConfig(source="simulated"), clock, _State()).is_simulated

    alp = build_service(
        MarketDataConfig(source="alpaca", alpaca_key_id="k", alpaca_secret="s", finnhub_key="f"),
        clock,
        _State(),
    )
    assert not alp.is_simulated and alp.supervisor is not None
    assert isinstance(alp.supervisor.stream, AlpacaStream) and isinstance(alp.supervisor.rest, AlpacaRest)
    assert isinstance(alp.supervisor.fallback, FinnhubRest)
    fin = build_service(MarketDataConfig(source="finnhub", finnhub_key="f"), clock, _State())
    assert fin.supervisor is not None and fin.supervisor.stream is None
    assert isinstance(fin.supervisor.rest, FinnhubRest) and fin.supervisor.fallback is None
    assert fin.supervisor.timing.poll_interval_s == MarketDataConfig().finnhub_interval_s


def test_config_reads_env_knobs() -> None:
    from app.core.config import Settings

    cfg = MarketDataConfig.from_settings(
        Settings(auth_mode="dev"),
        {
            "MARKETDATA_SIM_SEED": "7",
            "MARKETDATA_SIM_TICK_S": "0.5",
            "MARKETDATA_AUTOSTART": "off",
            "MARKETDATA_HEARTBEAT_S": "bad",
        },
    )
    assert (cfg.sim_seed, cfg.sim_tick_s, cfg.autostart, cfg.heartbeat_s) == (7, 0.5, False, 15.0)
    assert Decimal("1") == 1
