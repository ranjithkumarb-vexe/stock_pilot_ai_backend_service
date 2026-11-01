"""REST endpoints: /quotes, /quotes/{s}/bars, /market/status, dev controls (needs the Postgres test DB)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import jwt
import pytest

from app.core.clock import FakeClock

pytestmark = pytest.mark.db

FRI_OPEN = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
SAT = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)


async def test_endpoints_require_auth(md_app: Callable[..., Any]) -> None:
    async with md_app() as m:
        for path in ("/v1/quotes?symbols=AAPL", "/v1/quotes/AAPL/bars", "/v1/market/status"):
            r = await m.client.get(path)
            assert r.status_code == 401 and r.json()["code"] == "unauthenticated", path
        for path, body in (("/v1/dev/market", {"open": True}), ("/v1/dev/feed", {"state": "stale"})):
            assert (await m.client.post(path, json=body)).status_code == 401


async def test_quotes_shape_validation_and_unsupported_symbols(
    md_app: Callable[..., Any], seed_symbols: Callable[[], Any], make_user: Any, auth_headers: Any
) -> None:
    await seed_symbols()
    uid = await make_user()
    async with md_app(clock=FakeClock(FRI_OPEN)) as m:
        h = auth_headers(uid)
        r = await m.client.get("/v1/quotes?symbols=aapl,MSFT,AAPL", headers=h)
        assert r.status_code == 200
        items = r.json()["items"]
        assert [i["symbol"] for i in items] == ["AAPL", "MSFT"]  # upper-cased, de-duplicated, order kept
        q = items[0]
        assert set(q) >= {
            "symbol",
            "price",
            "prev_close",
            "change_pct",
            "bid",
            "ask",
            "ts",
            "source",
            "session",
            "freshness",
        }
        assert q["source"] == "simulated" and q["session"] == "regular" and q["freshness"] == "live"
        assert isinstance(q["price"], float) and q["ts"].endswith("Z")
        assert q["change_pct"] == pytest.approx((q["price"] / q["prev_close"] - 1) * 100, abs=0.011)

        bad = await m.client.get("/v1/quotes?symbols=AAPL,NOPE", headers=h)
        assert bad.status_code == 422 and bad.json()["code"] == "unsupported_symbol"
        assert bad.json()["details"]["symbols"] == ["NOPE"]
        for qs in ("symbols=", "symbols=AA PL", "symbols=" + ",".join(f"A{i}" for i in range(51))):
            r = await m.client.get(f"/v1/quotes?{qs}", headers=h)
            assert r.status_code == 422 and r.json()["code"] == "validation_error", qs
        assert (await m.client.get("/v1/quotes", headers=h)).status_code == 422


async def test_daily_bars_come_from_prices_daily_with_source_label(
    md_app: Callable[..., Any],
    seed_symbols: Callable[[], Any],
    make_user: Any,
    auth_headers: Any,
    db_admin: Any,
) -> None:
    await seed_symbols()
    await db_admin.execute("delete from prices_daily where symbol = 'KO'")
    for i in range(1, 6):
        await db_admin.execute(
            "insert into prices_daily (symbol, date, open, high, low, close, volume) values ('KO', $1, 60, 62, 59, $2, 1000)",
            date(2026, 10, 20 + i),
            60 + i,
        )
    uid = await make_user()
    async with md_app() as m:
        h = auth_headers(uid)
        body = (await m.client.get("/v1/quotes/KO/bars?interval=1d&limit=3", headers=h)).json()
        assert body["symbol"] == "KO" and body["interval"] == "1d" and body["source"] == "prices_daily"
        assert [b["close"] for b in body["bars"]] == [63, 64, 65]  # most recent 3, oldest first
        assert body["bars"][0]["time"] == "2026-10-23T00:00:00Z" and body["bars"][0]["volume"] == 1000
        empty = (await m.client.get("/v1/quotes/MSFT/bars?interval=1d", headers=h)).json()
        assert empty["bars"] == []
        assert (await m.client.get("/v1/quotes/KO/bars?interval=1h", headers=h)).status_code == 422
        assert (await m.client.get("/v1/quotes/NOPE/bars", headers=h)).json()["code"] == "unsupported_symbol"
    await db_admin.execute("delete from prices_daily where symbol = 'KO'")


async def test_five_minute_bars_are_simulated_labelled_and_end_at_the_quote(
    md_app: Callable[..., Any], seed_symbols: Callable[[], Any], make_user: Any, auth_headers: Any
) -> None:
    await seed_symbols()
    uid = await make_user()
    async with md_app(clock=FakeClock(FRI_OPEN)) as m:
        h = auth_headers(uid)
        quote = (await m.client.get("/v1/quotes?symbols=NVDA", headers=h)).json()["items"][0]
        body = (await m.client.get("/v1/quotes/NVDA/bars?interval=5m", headers=h)).json()
        assert body["source"] == "simulated" and body["interval"] == "5m"
        bars = body["bars"]
        assert 0 < len(bars) <= 78
        assert bars[-1]["close"] == quote["price"]
        assert all(b["low"] <= b["open"] <= b["high"] and b["low"] <= b["close"] <= b["high"] for b in bars)
        again = (await m.client.get("/v1/quotes/NVDA/bars?interval=5m", headers=h)).json()
        assert again["bars"] == bars  # deterministic


async def test_market_status_follows_calendar_holiday_and_dst(
    md_app: Callable[..., Any], make_user: Any, auth_headers: Any
) -> None:
    uid = await make_user()
    h = auth_headers(uid)
    async with md_app(clock=FakeClock(FRI_OPEN)) as m:
        s = (await m.client.get("/v1/market/status", headers=h)).json()
        assert s["open"] is True and s["session"] == "regular" and s["override"] is None
        assert s["next_close"] == "2026-10-30T20:00:00Z" and s["next_open"] == "2026-11-02T14:30:00Z"
        assert s["simulated"] is True and s["source"] == "simulated" and s["feed_state"] == "live"
    async with md_app(clock=FakeClock(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))) as m:  # Thanksgiving
        s = (await m.client.get("/v1/market/status", headers=h)).json()
        assert s["open"] is False and s["session"] == "closed" and s["next_open"] == "2026-11-27T14:30:00Z"
    async with md_app(clock=FakeClock(datetime(2026, 11, 27, 16, 0, tzinfo=UTC))) as m:  # half day
        s = (await m.client.get("/v1/market/status", headers=h)).json()
        assert s["early_close"] is True and s["next_close"] == "2026-11-27T18:00:00Z"


async def test_dev_market_override_drives_session_freshness_and_status(
    md_app: Callable[..., Any], seed_symbols: Callable[[], Any], make_user: Any, auth_headers: Any
) -> None:
    await seed_symbols()
    uid = await make_user()
    h = auth_headers(uid)
    async with md_app(clock=FakeClock(SAT)) as m:  # Saturday: closed by the calendar
        q = (await m.client.get("/v1/quotes?symbols=AAPL", headers=h)).json()["items"][0]
        assert q["session"] == "closed" and q["freshness"] == "closed"
        r = await m.client.post("/v1/dev/market", json={"open": True}, headers=h)
        assert r.status_code == 200 and r.json() == {"open": True, "override": True, "session": "regular"}
        assert m.app.state.market_override is True  # what stream A's paper engine reads
        q = (await m.client.get("/v1/quotes?symbols=AAPL", headers=h)).json()["items"][0]
        assert q["session"] == "regular" and q["freshness"] in {"live", "delayed"}
        assert (await m.client.get("/v1/market/status", headers=h)).json()["override"] is True
        r = await m.client.post("/v1/dev/market", json={"open": False}, headers=h)
        assert r.json()["session"] == "closed" and m.app.state.market_override is False
        r = await m.client.post("/v1/dev/market", json={"open": None}, headers=h)  # clear
        assert r.json()["override"] is None and m.app.state.market_override is None
        assert (await m.client.post("/v1/dev/market", json={"open": "maybe"}, headers=h)).status_code == 422


async def test_dev_feed_state_overrides_freshness_of_every_quote(
    md_app: Callable[..., Any], seed_symbols: Callable[[], Any], make_user: Any, auth_headers: Any
) -> None:
    await seed_symbols()
    uid = await make_user()
    h = auth_headers(uid)
    async with md_app(clock=FakeClock(FRI_OPEN)) as m:
        expected = {"live": "live", "delayed": "delayed", "reconnecting": "delayed", "stale": "stale"}
        for state, fresh in expected.items():
            r = await m.client.post("/v1/dev/feed", json={"state": state}, headers=h)
            assert r.status_code == 200 and r.json() == {"state": state}
            items = (await m.client.get("/v1/quotes?symbols=AAPL,MSFT,NVDA", headers=h)).json()["items"]
            assert {i["freshness"] for i in items} == {fresh}, state
            assert (await m.client.get("/v1/market/status", headers=h)).json()["feed_state"] == state
        # stale blocks unusual-move alerts and market fills (policy helpers used by alerts / paper engine)
        await m.client.post("/v1/dev/feed", json={"state": "stale"}, headers=h)
        assert m.svc.fills_blocked("AAPL") and not m.svc.move_alerts_allowed("AAPL")
        await m.client.post("/v1/dev/feed", json={"state": "live"}, headers=h)
        assert not m.svc.fills_blocked("AAPL") and m.svc.move_alerts_allowed("AAPL")
        assert (await m.client.post("/v1/dev/feed", json={"state": "broken"}, headers=h)).status_code == 422


async def test_dev_controls_are_404_outside_dev_mode(
    md_app: Callable[..., Any], make_user: Any, settings: Any
) -> None:
    uid = await make_user()
    secret = "s" * 40
    token = jwt.encode(
        {"sub": str(uid), "aud": "authenticated", "exp": int(datetime.now(UTC).timestamp()) + 600},
        secret,
        algorithm="HS256",
    )
    h = {"Authorization": f"Bearer {token}"}
    async with md_app(auth_mode="supabase", supabase_jwt_secret=secret) as m:
        for path, body in (("/v1/dev/market", {"open": True}), ("/v1/dev/feed", {"state": "stale"})):
            r = await m.client.post(path, json=body, headers=h)
            assert r.status_code == 404 and r.json()["code"] == "not_found"
        assert m.app.state.market_override is None  # nothing changed
        assert (await m.client.get("/v1/market/status", headers=h)).status_code == 200


async def test_app_state_hooks_for_the_paper_engine(md_app: Callable[..., Any]) -> None:
    async with md_app(market_override=False) as m:
        st = m.app.state
        assert st.market_override is False  # honoured, not clobbered, by the lifespan
        q = await st.price_source.get_last("AAPL")
        assert q.source == "simulated" and q.session == "closed" and q.freshness == "closed"
        assert q.prev_close is not None and q.bid is not None
        st.market_override = True  # A's tests flip this directly
        assert (await st.price_source.get_last("AAPL")).session == "regular"
        from app.core.pricesource import PriceSource, QuoteUnavailableError

        assert isinstance(st.price_source, PriceSource)
        assert st.stream_hub is m.svc.hub
        _ = QuoteUnavailableError


async def test_price_source_subscribe_yields_ticks(md_app: Callable[..., Any]) -> None:
    async with md_app(market_override=True) as m:
        got = []
        async for q in m.app.state.price_source.subscribe(["AAPL"]):
            got.append(q)
            if len(got) == 2:
                break
        assert got[0].symbol == "AAPL" and got[1].ts >= got[0].ts
        await asyncio.sleep(0)
