"""SSE `GET /v1/stream?token=`: auth, per-user filtering, tick latency, heartbeat, Last-Event-ID replay."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.marketdata.config import MarketDataConfig
from tests.marketdata.conftest import SseSession, mint_stream_token

pytestmark = pytest.mark.db

FAST = MarketDataConfig(sim_tick_s=0.1, heartbeat_s=0.25, stream_symbols_refresh_s=0.4)


async def give(db_admin: Any, uid: UUID, watch: list[str], hold: list[str] | None = None) -> None:
    for s in watch:
        await db_admin.execute(
            "insert into watchlist_items (user_id, symbol) values ($1, $2) on conflict do nothing", uid, s
        )
    if hold:
        pid = await db_admin.fetchval(
            "insert into portfolios (user_id, name, starting_cash) values ($1, 'Main', 1000) returning id",
            uid,
        )
        for s in hold:
            await db_admin.execute(
                "insert into positions (portfolio_id, symbol, user_id, qty, avg_cost) values ($1, $2, $3, 5, 10)",
                pid,
                s,
                uid,
            )


async def open_stream(m: Any, auth: dict[str, str], **kw: Any) -> SseSession:
    token = await mint_stream_token(m.client, auth)
    return await SseSession(m.app, token, **kw).open()


async def test_rejects_missing_invalid_and_reused_tokens(
    md_app: Callable[..., Any], seed_symbols: Callable[[], Any], make_user: Any, auth_headers: Any
) -> None:
    await seed_symbols()
    uid = await make_user()
    async with md_app(cfg=FAST, market_override=True) as m:
        for url in (
            "/v1/stream",
            "/v1/stream?token=",
            "/v1/stream?token=nope",
            "/v1/stream?token=" + "x" * 300,
        ):
            r = await m.client.get(url)
            assert r.status_code == 401 and r.json()["code"] == "unauthenticated", url
        h = auth_headers(uid)
        token = await mint_stream_token(m.client, h)
        first = await SseSession(m.app, token).open()
        assert first.status == 200
        second = await SseSession(m.app, token).open()  # single use
        assert second.status == 401
        await first.close()
        await second.close()


async def test_stream_headers_snapshot_and_only_the_users_symbols(
    md_app: Callable[..., Any],
    seed_symbols: Callable[[], Any],
    make_user: Any,
    auth_headers: Any,
    db_admin: Any,
) -> None:
    await seed_symbols()
    a, b = await make_user(), await make_user()
    await give(db_admin, a, ["AAPL"], hold=["MSFT"])  # watchlist plus holdings
    await give(db_admin, b, ["NVDA"])
    async with md_app(cfg=FAST, market_override=True) as m:
        sa = await open_stream(m, auth_headers(a))
        sb = await open_stream(m, auth_headers(b))
        assert sa.status == 200 and sa.headers["content-type"].startswith("text/event-stream")
        assert "no-cache" in sa.headers["cache-control"] and sa.headers["x-accel-buffering"] == "no"
        assert "x-trace-id" in sa.headers
        await asyncio.sleep(0.8)  # several ticks
        for s in (sa, sb):
            await s.next_events(timeout=0.2, want=100)
        symbols_a = {e.data["symbol"] for e in sa.events if e.event == "quote"}
        symbols_b = {e.data["symbol"] for e in sb.events if e.event == "quote"}
        assert symbols_a == {"AAPL", "MSFT"} and symbols_b == {"NVDA"}
        ids = [e.id for e in sa.events if e.id is not None]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)  # monotonic ids
        ev = next(e for e in sa.events if e.event == "quote")
        assert set(ev.data) >= {
            "symbol",
            "price",
            "prev_close",
            "change_pct",
            "ts",
            "source",
            "session",
            "freshness",
        }
        assert ev.data["source"] == "simulated"
        await sa.close()
        await sb.close()
        await asyncio.sleep(0.1)
        assert m.svc.hub.subscriber_count == 0  # disconnect cleaned up


async def test_tick_reaches_the_client_within_two_seconds_at_the_default_1s_tick(
    md_app: Callable[..., Any],
    seed_symbols: Callable[[], Any],
    make_user: Any,
    auth_headers: Any,
    db_admin: Any,
) -> None:
    await seed_symbols()
    uid = await make_user()
    await give(db_admin, uid, ["AAPL"])
    default = replace(MarketDataConfig(), heartbeat_s=15.0)  # sim_tick_s == 1.0 (SPEC: ~1 s)
    assert default.sim_tick_s == 1.0
    async with md_app(cfg=default, market_override=True) as m:
        s = await open_stream(m, auth_headers(uid))
        snap = await s.next_events(timeout=2, want=1)  # initial snapshot
        assert snap and snap[0].event == "quote"
        started = time.monotonic()
        snap_id = snap[0].id or 0
        tick = await s.until(lambda e: e.event == "quote" and (e.id or 0) > snap_id, timeout=3)
        elapsed = time.monotonic() - started
        assert tick is not None, "no tick received"
        assert elapsed <= 2.0, f"tick took {elapsed:.2f}s"
        assert tick.data["ts"] > snap[0].data["ts"]
        await s.close()


async def test_heartbeat_event_has_an_id(
    md_app: Callable[..., Any], make_user: Any, auth_headers: Any
) -> None:
    uid = await make_user()
    async with md_app(cfg=FAST, market_override=False) as m:  # nothing ticks while closed
        s = await open_stream(m, auth_headers(uid))
        hb = await s.until(lambda e: e.event == "heartbeat", timeout=2)
        assert hb is not None and hb.id is not None and "ts" in hb.data
        hb2 = await s.until(lambda e: e.event == "heartbeat" and e.id != hb.id, timeout=2)
        assert hb2 is not None and hb2.id > hb.id  # type: ignore[operator]
        await s.close()


async def test_alert_and_proposal_events_go_only_to_their_owner(
    md_app: Callable[..., Any], make_user: Any, auth_headers: Any
) -> None:
    a, b = await make_user(), await make_user()
    async with md_app(cfg=FAST, market_override=False) as m:
        sa, sb = await open_stream(m, auth_headers(a)), await open_stream(m, auth_headers(b))
        m.app.state.stream_hub.publish_user_event(
            a, "alert", {"id": "al-1", "title": "Position cap breached"}
        )
        m.app.state.stream_hub.publish_user_event(a, "proposal", {"id": "pr-1"})
        got_alert = await sa.until(lambda e: e.event == "alert", timeout=2)
        got_prop = await sa.until(lambda e: e.event == "proposal", timeout=2)
        assert got_alert is not None and got_alert.data["id"] == "al-1"
        assert got_prop is not None and got_prop.data == {"id": "pr-1"}
        await sb.next_events(timeout=0.6, want=50)
        assert not [e for e in sb.events if e.event in {"alert", "proposal"}]
        await sa.close()
        await sb.close()


async def test_last_event_id_replays_missed_events_via_header_and_query(
    md_app: Callable[..., Any],
    seed_symbols: Callable[[], Any],
    make_user: Any,
    auth_headers: Any,
    db_admin: Any,
) -> None:
    await seed_symbols()
    uid = await make_user()
    await give(db_admin, uid, ["AAPL", "MSFT"])
    h = auth_headers(uid)
    async with md_app(cfg=FAST, market_override=True) as m:
        s1 = await open_stream(m, h)
        await s1.next_events(timeout=0.5, want=3)
        last = max(e.id for e in s1.events if e.id is not None)
        await s1.close()
        m.app.state.stream_hub.publish_user_event(uid, "alert", {"id": "missed"})  # while disconnected
        await asyncio.sleep(0.6)  # ticks keep flowing while the client is away

        s2 = await open_stream(m, h, headers={"Last-Event-ID": str(last)})
        await s2.next_events(timeout=0.4, want=20)
        replayed = [e for e in s2.events if e.id is not None]
        assert replayed and all(e.id > last for e in replayed)  # type: ignore[operator]
        quotes = [e for e in replayed if e.event == "quote"]
        # replay is coalesced: at most one (latest) quote per symbol is in the first burst
        first_burst = quotes[:2]
        assert {e.data["symbol"] for e in first_burst} <= {"AAPL", "MSFT"}
        assert any(e.event == "alert" and e.data["id"] == "missed" for e in replayed)
        await s2.close()

        s3 = await open_stream(
            m, h, extra_qs=f"&last_event_id={last}"
        )  # EventSource cannot set headers on reconnect
        await s3.next_events(timeout=0.4, want=5)
        assert any(e.event == "alert" and e.data["id"] == "missed" for e in s3.events)
        await s3.close()


async def test_watchlist_additions_are_picked_up_while_streaming(
    md_app: Callable[..., Any],
    seed_symbols: Callable[[], Any],
    make_user: Any,
    auth_headers: Any,
    db_admin: Any,
) -> None:
    await seed_symbols()
    uid = await make_user()
    await give(db_admin, uid, ["AAPL"])
    async with md_app(cfg=FAST, market_override=True) as m:
        s = await open_stream(m, auth_headers(uid))
        await s.next_events(timeout=0.5, want=3)
        assert {e.data["symbol"] for e in s.events if e.event == "quote"} == {"AAPL"}
        await give(db_admin, uid, ["KO"])
        got = await s.until(lambda e: e.event == "quote" and e.data["symbol"] == "KO", timeout=3)
        assert got is not None
        await s.close()


async def test_at_most_five_concurrent_streams_per_user(
    md_app: Callable[..., Any], make_user: Any, auth_headers: Any
) -> None:
    uid = await make_user()
    h = auth_headers(uid)
    async with md_app(cfg=FAST, market_override=False) as m:
        sessions = [await open_stream(m, h) for _ in range(5)]
        assert all(s.status == 200 for s in sessions)
        extra = await open_stream(m, h)
        assert extra.status == 429
        await extra.close()
        for s in sessions:
            await s.close()
        await asyncio.sleep(0.1)
        again = await open_stream(m, h)
        assert again.status == 200
        await again.close()
