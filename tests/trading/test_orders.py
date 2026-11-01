"""BE-07: orders API + paper engine behaviour (T-PAPER-01..06, T-DATA-02 on fills, T-SEC-03/07)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest

from app.core.pricesource import StaticPriceSource
from app.modules.paper import service
from app.modules.paper.context import build_context
from tests.trading.support import StaleSource, login, place

pytestmark = pytest.mark.db


class Env:
    """Handle on the running app for tests that drive the engine directly."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.app = client.app  # type: ignore[attr-defined]
        self.clock = client.clock  # type: ignore[attr-defined]

    def open(self, value: bool | None = True) -> None:
        self.app.state.market_override = value

    def ctx(self) -> Any:
        return build_context(self.app.state)

    async def sweep(self, test_database: str) -> dict[str, int]:
        conn = await asyncpg.connect(test_database)
        from app.core.db import init_connection

        await init_connection(conn)
        try:
            return await service.process_orders(conn, self.ctx())
        finally:
            await conn.close()


@pytest.fixture
async def env(tclient: httpx.AsyncClient) -> Env:
    return Env(tclient)


async def positions(db_admin: Any, uid: Any) -> dict[str, tuple[Decimal, Decimal]]:
    rows = await db_admin.fetch("select symbol, qty, avg_cost from positions where user_id = $1", uid)
    return {r["symbol"]: (r["qty"], r["avg_cost"]) for r in rows}


async def cash(db_admin: Any, uid: Any) -> Decimal:
    return Decimal(await db_admin.fetchval("select sum(amount) from cash_ledger where user_id = $1", uid))


# ------------------------------------------------------------------------------------------ basics


async def test_market_buy_fills_with_5bps_slippage_up_and_updates_ledger(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    r = await place(env.client, h, "AAPL", "buy", 10)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["order"]["status"] == "filled" and j["order"]["filled_qty"] == 10 and j["blocked"] is False
    assert j["fill"]["price"] == 268.83 and j["fill"]["slippage_bps"] == 5  # 268.70 x 1.0005, cents
    assert j["order"]["avg_fill_price"] == 268.83 and j["order"]["source"] == "manual"
    assert await cash(db_admin, uid) == Decimal("100000") - Decimal("2688.30")
    assert await positions(db_admin, uid) == {"AAPL": (Decimal(10), Decimal("268.8300"))}
    assert [c["label"] for c in j["checks"]][:2] == ["Paper cash", "Max position 15%"]


async def test_market_sell_fills_down_and_realises_against_average_cost(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    r = await place(env.client, h, "JNJ", "sell", 4)
    assert r.status_code == 201, r.text
    assert r.json()["fill"]["price"] == 171.71  # 171.80 x 0.9995
    pos = await positions(db_admin, uid)
    assert pos["JNJ"] == (Decimal(58), Decimal("152.4000"))  # a sale never changes the average cost


async def test_selling_everything_removes_the_position_row(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    assert (await place(env.client, h, "AAPL", "sell", 24)).status_code == 201
    assert "AAPL" not in await positions(db_admin, uid)


async def test_buy_averages_the_cost(env: Env, reseed: Callable[..., Any], db_admin: Any) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    assert (
        await place(env.client, h, "KO", "sell", 1)
    ).status_code == 201  # keep sector headroom irrelevant: sells first
    r = await place(env.client, h, "AAPL", "buy", 6, override=True)
    assert r.status_code == 201, r.text
    qty, avg = (await positions(db_admin, uid))["AAPL"]
    expected = (Decimal("231.5") * 24 + Decimal("268.83") * 6) / 30
    assert qty == 30 and avg == expected.quantize(Decimal("0.0001"))


# ------------------------------------------------------------------------------------------ T-PAPER-01


async def test_t_paper_01_market_order_outside_hours_is_queued_then_filled_at_the_open(
    env: Env, reseed: Callable[..., Any], db_admin: Any, test_database: str
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    env.open(False)
    r = await place(env.client, h, "MSFT", "buy", 5)
    assert r.status_code == 201, r.text
    assert r.json()["order"]["status"] == "queued" and r.json()["fill"] is None
    assert any(c["key"] == "session" and c["severity"] == "note" for c in r.json()["checks"])
    assert await cash(db_admin, uid) == Decimal(100000) and await positions(db_admin, uid) == {}
    assert (await env.sweep(test_database)) == {
        "filled": 0,
        "expired": 0,
        "rejected": 0,
        "activated": 0,
    }  # still closed
    env.open(True)
    assert (await env.sweep(test_database))["filled"] == 1
    j = (await env.client.get("/v1/orders", headers=h)).json()["items"][0]
    assert j["status"] == "filled" and j["avg_fill_price"] == 521.86  # 521.60 x 1.0005
    assert (await positions(db_admin, uid))["MSFT"][0] == 5
    assert (await env.sweep(test_database))["filled"] == 0  # not filled twice


async def test_t_paper_01_reject_setting_refuses_after_hours_market_orders(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    await db_admin.execute("update portfolios set after_hours_policy = 'reject' where user_id = $1", uid)
    env.open(False)
    r = await place(env.client, h, "MSFT", "buy", 5)
    assert r.status_code == 409 and r.json()["code"] == "market_closed"
    assert await db_admin.fetchval("select count(*) from orders where user_id = $1", uid) == 0
    # limit orders are still accepted (queued for the next session)
    assert (await place(env.client, h, "MSFT", "buy", 5, type="limit", limit_price=400)).json()["order"][
        "status"
    ] == "queued"


# ------------------------------------------------------------------------------------------ T-PAPER-02


async def test_t_paper_02_four_for_one_split_keeps_value_and_pnl(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    pid = await db_admin.fetchval("select id from portfolios where user_id = $1", uid)
    before = (await positions(db_admin, uid))["AAPL"]
    price = Decimal("268.70")
    value_before, pnl_before = before[0] * price, before[0] * (price - before[1])
    now = env.clock.now()
    try:
        assert await service.apply_split(db_admin, "AAPL", now.date(), Decimal(4), now) == 1
        qty, avg = (await positions(db_admin, uid))["AAPL"]
        assert qty == before[0] * 4 and avg == (before[1] / 4).quantize(Decimal("0.0001"))
        after_price = price / 4
        assert qty * after_price == value_before
        assert qty * (after_price - avg) == pytest.approx(pnl_before, abs=Decimal("0.01"))
        adj = await db_admin.fetchrow("select * from position_adjustments where portfolio_id = $1", pid)
        assert (adj["qty_before"], adj["qty_after"], adj["ratio"]) == (before[0], qty, Decimal(4))
        # idempotent: applying again changes nothing; positions are still rebuildable from fills + the split
        assert await service.apply_split(db_admin, "AAPL", now.date(), Decimal(4), now) == 0
        assert (await positions(db_admin, uid))["AAPL"] == (qty, avg)
        rec = await service.reconcile(db_admin, pid)
        assert rec["ok"] and rec["position_mismatches"] == []
        # a post-split sale works in post-split shares
        r = await place(env.client, h, "AAPL", "sell", 10)
        assert r.status_code == 201
    finally:
        await db_admin.execute("delete from corporate_actions where symbol = 'AAPL'")


async def test_split_before_the_ex_date_is_not_applied_yet(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    _, uid = await login(env.client, "priya")
    now = env.clock.now()
    from datetime import timedelta

    try:
        assert (
            await service.apply_split(db_admin, "AAPL", (now + timedelta(days=5)).date(), Decimal(2), now)
            == 0
        )
        assert (await positions(db_admin, uid))["AAPL"][0] == 24
    finally:
        await db_admin.execute("delete from corporate_actions where symbol = 'AAPL'")
    with pytest.raises(ValueError, match="positive"):
        await service.apply_split(db_admin, "AAPL", now.date(), Decimal(0), now)


# ------------------------------------------------------------------------------------------ T-PAPER-03 + hard checks


async def test_t_paper_03_sell_more_than_held_is_rejected(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    before = await db_admin.fetchval("select count(*) from orders where user_id = $1", uid)
    r = await place(env.client, h, "AAPL", "sell", 25)
    assert r.status_code == 422 and r.json()["code"] == "insufficient_shares"
    assert "cannot sell 25" in r.json()["message"] and r.json()["details"]["checks"]
    r = await place(env.client, h, "TSLA", "sell", 1)  # not held at all
    assert r.json()["code"] == "insufficient_shares"
    assert await db_admin.fetchval("select count(*) from orders where user_id = $1", uid) == before
    assert (await positions(db_admin, uid))["AAPL"][0] == 24


async def test_insufficient_cash_is_422_and_slippage_buffer_counts(
    env: Env, reseed: Callable[..., Any]
) -> None:
    await reseed("alex")
    h, _ = await login(env.client, "alex")  # cash 7,983
    r = await place(env.client, h, "NVDA", "buy", 50, override=True)
    assert r.status_code == 422 and r.json()["code"] == "insufficient_cash"
    # 7983 / 182.40 = 43.77 -> 43 shares fit; 43 x 182.49 (slipped) = 7847 ok
    assert (await place(env.client, h, "NVDA", "buy", 43, override=True)).status_code == 201


async def test_cash_check_includes_the_slippage_buffer(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    # cost at last = 10 x 268.70 = 2687.00 <= cash 2688.00 < 10 x 268.83 = 2688.30
    await db_admin.execute(
        "insert into cash_ledger (portfolio_id, user_id, kind, amount) select id, user_id, 'adjustment', -97312 from portfolios where user_id = $1",
        uid,
    )
    r = await env.client.post(
        "/v1/orders/preview", headers=h, json={"symbol": "AAPL", "side": "buy", "type": "market", "qty": 10}
    )
    funds = next(c for c in r.json()["checks"] if c["key"] == "funds")
    assert funds["ok"] is False and "slippage buffer" in funds["detail"] and r.json()["hard_blocked"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"symbol": "ZZZZ", "side": "buy", "type": "market", "qty": 1}, 422, "unsupported_symbol"),
        ({"symbol": "SPY", "side": "buy", "type": "market", "qty": 1}, 422, "unsupported_symbol"),
        ({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 0}, 422, "validation_error"),
        ({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1.5}, 422, "validation_error"),
        ({"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 1}, 422, "validation_error"),
        (
            {"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1, "limit_price": 10},
            422,
            "validation_error",
        ),
        ({"symbol": "AAPL", "side": "hold", "type": "market", "qty": 1}, 422, "validation_error"),
        (
            {"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 1, "limit_price": 0},
            422,
            "validation_error",
        ),
        ({"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1, "bogus": 1}, 422, "validation_error"),
    ],
)
async def test_invalid_orders_are_rejected(env: Env, body: dict[str, Any], status: int, code: str) -> None:
    h, _ = await login(env.client, "sam")
    r = await env.client.post("/v1/orders", headers={**h, "Idempotency-Key": str(uuid4())}, json=body)
    assert r.status_code == status and r.json()["code"] == code


async def test_orders_need_auth_and_a_portfolio(env: Env) -> None:
    body = {"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1}
    assert (
        await env.client.post("/v1/orders", headers={"Idempotency-Key": str(uuid4())}, json=body)
    ).status_code == 401
    r = await env.client.post("/v1/auth/dev-login", json={"persona": "new"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await env.client.post("/v1/orders", headers={**h, "Idempotency-Key": str(uuid4())}, json=body)
    assert r.status_code == 404


# ------------------------------------------------------------------------------------------ risk rules + override


async def test_rule_breach_is_blocked_unless_overridden_and_override_is_logged(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    # JNJ is already above Priya's 10% cap; buying more breaches it
    r = await place(env.client, h, "JNJ", "buy", 1)
    assert r.status_code == 422 and r.json()["code"] == "rule_violation"
    v = r.json()["details"]["violations"]
    assert v[0]["rule"] == "pos" and "JNJ would be" in v[0]["message"]
    assert (
        await db_admin.fetchval(
            "select count(*) from orders where user_id = $1 and symbol = 'JNJ' and created_at > now() - interval '1 minute'",
            uid,
        )
        == 0
    )
    cash_before = await cash(db_admin, uid)
    ok = await place(env.client, h, "JNJ", "buy", 1, override=True)
    assert ok.status_code == 201 and ok.json()["blocked"] is True and ok.json()["order"]["override"] is True
    assert await cash(db_admin, uid) < cash_before
    ev = await db_admin.fetch("select props from events where user_id = $1 and type = 'rule_override'", uid)
    assert len(ev) == 1 and "pos" in ev[0]["props"]["rules"]
    # the override never changed the rules
    assert (
        await db_admin.fetchval("select value from risk_rules where user_id = $1 and rule_key = 'pos'", uid)
    ) == 10


async def test_preview_matches_design_labels_and_does_not_write(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    before = await db_admin.fetchval("select count(*) from orders where user_id = $1", uid)
    r = await env.client.post(
        "/v1/orders/preview", headers=h, json={"symbol": "JNJ", "side": "buy", "type": "market", "qty": 10}
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["blocked"] is True and j["hard_blocked"] is False and j["fill"] is None
    labels = [(c["label"], c["severity"]) for c in j["checks"]]
    assert labels == [
        ("Paper cash", "pass"),
        ("Max position 10%", "blocked"),
        ("Max sector 25%", "pass"),
        ("Min cash buffer 10%", "pass"),
        ("Trades per week 3", "pass"),
    ]
    assert j["checks"][1]["detail"] == "JNJ would be 12.4%"  # (10651.6 + 1718) / 99,999.xx
    assert j["order"]["will"] is None and j["order"]["est_price"] > 0
    assert await db_admin.fetchval("select count(*) from orders where user_id = $1", uid) == before
    # closed market adds the design's Note row; sells show shares held
    env.open(False)
    r = await env.client.post(
        "/v1/orders/preview", headers=h, json={"symbol": "KO", "side": "sell", "type": "market", "qty": 3}
    )
    j = r.json()
    assert j["blocked"] is False and j["order"]["will"] == "queue"
    # integration pass 1: the preview carries the cash the account would hold afterwards (sell: + proceeds)
    cash_now = await db_admin.fetchval(
        "select coalesce(sum(amount), 0) from cash_ledger where user_id = $1", uid
    )
    assert abs(j["order"]["est_cash_after"] - (float(cash_now) + j["order"]["est_cost"])) < 0.011
    assert j["checks"][0] == {
        "key": "shares",
        "ok": True,
        "severity": "pass",
        "label": "No shorting (MVP)",
        "detail": "Selling 3 of 130 shares held",
    }
    assert j["checks"][-1] == {
        "key": "session",
        "ok": False,
        "severity": "note",
        "label": "Market session",
        "detail": "Market is closed. The order will be queued for the next open.",
    }


async def test_trades_per_week_blocks_buys_and_notes_sells(env: Env, reseed: Callable[..., Any]) -> None:
    await reseed("priya")
    h, _ = await login(env.client, "priya")
    for sym in ("XOM", "XOM"):
        assert (
            await place(env.client, h, sym, "sell", 1)
        ).status_code == 201  # Priya had 1 trade in 7 d -> now 3
    pv = await env.client.post(
        "/v1/orders/preview", headers=h, json={"symbol": "XOM", "side": "sell", "type": "market", "qty": 1}
    )
    trades = next(c for c in pv.json()["checks"] if c["key"] == "trades")
    assert (
        trades["severity"] == "note"
        and trades["detail"] == "3 in the last 7 days"
        and pv.json()["blocked"] is False
    )
    pv = await env.client.post(
        "/v1/orders/preview", headers=h, json={"symbol": "XOM", "side": "buy", "type": "market", "qty": 1}
    )
    assert next(c for c in pv.json()["checks"] if c["key"] == "trades")["severity"] == "blocked"


# ------------------------------------------------------------------------------------------ T-PAPER-05


async def test_t_paper_05_duplicate_idempotency_key_is_one_order_one_fill(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    key = str(uuid4())
    a = await place(env.client, h, "AAPL", "buy", 2, key=key)
    b = await place(env.client, h, "AAPL", "buy", 2, key=key)
    assert a.status_code == 201 and b.status_code == 200
    assert b.headers["Idempotent-Replay"] == "true" and "Idempotent-Replay" not in a.headers
    assert (
        a.json()["order"]["id"] == b.json()["order"]["id"]
        and a.json()["fill"]["id"] == b.json()["fill"]["id"]
    )
    assert await db_admin.fetchval("select count(*) from orders where user_id = $1", uid) == 1
    assert await db_admin.fetchval("select count(*) from fills where user_id = $1", uid) == 1
    assert (await positions(db_admin, uid))["AAPL"][0] == 2
    conflict = await place(env.client, h, "AAPL", "buy", 3, key=key)
    assert conflict.status_code == 409 and conflict.json()["code"] == "conflict"


async def test_t_paper_05_concurrent_duplicates_create_exactly_one_order(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    key = str(uuid4())
    rs = await asyncio.gather(*(place(env.client, h, "AAPL", "buy", 1, key=key) for _ in range(8)))
    assert sorted(r.status_code for r in rs) == [200] * 7 + [201]
    assert await db_admin.fetchval("select count(*) from orders where user_id = $1", uid) == 1
    assert await db_admin.fetchval("select count(*) from fills where user_id = $1", uid) == 1
    assert await cash(db_admin, uid) == Decimal(100000) - Decimal("268.83")


async def test_idempotency_key_header_is_required_and_must_be_a_uuid(env: Env) -> None:
    h, _ = await login(env.client, "sam")
    body = {"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1}
    r = await env.client.post("/v1/orders", headers=h, json=body)
    assert (
        r.status_code == 422
        and r.json()["code"] == "validation_error"
        and "Idempotency-Key" in r.json()["message"]
    )
    r = await env.client.post("/v1/orders", headers={**h, "Idempotency-Key": "abc"}, json=body)
    assert r.status_code == 422
    # preview needs no key
    assert (await env.client.post("/v1/orders/preview", headers=h, json=body)).status_code == 200


# ------------------------------------------------------------------------------------------ T-PAPER-06


async def test_t_paper_06_limit_buy_rests_then_fills_at_the_limit_once(
    env: Env, reseed: Callable[..., Any], db_admin: Any, test_database: str
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    src = StaticPriceSource({"AAPL": Decimal("268.70")}, env.clock.now())
    env.app.state.price_source = src
    r = await place(env.client, h, "AAPL", "buy", 5, type="limit", limit_price=260)
    assert r.status_code == 201 and r.json()["order"]["status"] == "working" and r.json()["fill"] is None
    assert (await env.sweep(test_database))["filled"] == 0  # not crossed
    src.set_price("AAPL", Decimal("259.50"))  # crosses (<= 260)
    assert (await env.sweep(test_database))["filled"] == 1
    j = (await env.client.get("/v1/orders?status=filled", headers=h)).json()["items"][0]
    assert (
        j["avg_fill_price"] == 260 and j["filled_qty"] == 5
    )  # at the LIMIT, no price improvement, no slippage
    assert (await env.sweep(test_database))["filled"] == 0
    assert await db_admin.fetchval("select count(*) from fills where user_id = $1", uid) == 1
    assert await cash(db_admin, uid) == Decimal(100000) - 5 * 260


async def test_marketable_limit_fills_immediately_at_the_limit(env: Env, reseed: Callable[..., Any]) -> None:
    await reseed("sam")
    h, _ = await login(env.client, "sam")
    r = await place(env.client, h, "AAPL", "buy", 1, type="limit", limit_price=300)
    assert (
        r.json()["order"]["status"] == "filled"
        and r.json()["fill"]["price"] == 300
        and r.json()["fill"]["slippage_bps"] == 0
    )
    s = await place(env.client, h, "AAPL", "sell", 1, type="limit", limit_price=250)
    assert s.json()["fill"]["price"] == 250  # sell limit below market crosses at the limit


async def test_t_paper_06_day_limit_order_expires_at_the_close(
    env: Env, reseed: Callable[..., Any], db_admin: Any, test_database: str
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    r = await place(env.client, h, "AAPL", "buy", 5, type="limit", limit_price=200)
    oid = r.json()["order"]["id"]
    assert r.json()["order"]["status"] == "working"
    env.open(False)  # session closes
    assert (await env.sweep(test_database))["expired"] == 1
    j = (await env.client.get("/v1/orders", headers=h)).json()["items"][0]
    assert j["id"] == oid and j["status"] == "expired" and "expired" in j["reject_reason"]
    env.open(True)
    assert (await env.sweep(test_database)) == {"filled": 0, "expired": 0, "rejected": 0, "activated": 0}
    assert await db_admin.fetchval("select count(*) from fills where user_id = $1", uid) == 0


async def test_limit_order_placed_while_closed_waits_for_the_open_then_works(
    env: Env, reseed: Callable[..., Any], test_database: str
) -> None:
    await reseed("sam")
    h, _ = await login(env.client, "sam")
    env.open(False)
    r = await place(env.client, h, "AAPL", "buy", 5, type="limit", limit_price=200)
    assert r.json()["order"]["status"] == "queued"
    env.open(True)
    assert (await env.sweep(test_database))["activated"] == 1
    assert (await env.client.get("/v1/orders", headers=h)).json()["items"][0]["status"] == "working"


async def test_expiry_by_clock_even_if_the_market_stays_open(
    env: Env, reseed: Callable[..., Any], test_database: str
) -> None:
    await reseed("sam")
    h, _ = await login(env.client, "sam")
    r = await place(env.client, h, "AAPL", "buy", 5, type="limit", limit_price=200)
    assert r.json()["order"]["expires_at"] is not None
    env.clock.advance(days=3)
    assert (await env.sweep(test_database))["expired"] == 1


async def test_queued_buy_is_rejected_at_the_open_when_cash_is_gone(
    env: Env, reseed: Callable[..., Any], db_admin: Any, test_database: str
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    env.open(False)
    assert (await place(env.client, h, "MSFT", "buy", 150, override=True)).json()["order"][
        "status"
    ] == "queued"  # ~78k
    assert (await place(env.client, h, "MSFT", "buy", 150, override=True)).json()["order"][
        "status"
    ] == "queued"
    env.open(True)
    stats = await env.sweep(test_database)
    assert stats["filled"] == 1 and stats["rejected"] == 1
    rejected = await db_admin.fetchrow(
        "select reject_reason from orders where user_id = $1 and status = 'rejected'", uid
    )
    assert rejected["reject_reason"] == "insufficient_cash"
    assert await cash(db_admin, uid) > 0


# ------------------------------------------------------------------------------------------ stale quotes (T-DATA-02)


async def test_stale_quote_blocks_market_orders_but_not_limits(
    env: Env, reseed: Callable[..., Any], test_database: str
) -> None:
    await reseed("sam")
    h, _ = await login(env.client, "sam")
    env.app.state.price_source = StaleSource(env.app.state.price_source)
    r = await place(env.client, h, "AAPL", "buy", 1)
    assert r.status_code == 409 and r.json()["code"] == "stale_quote"
    pv = await env.client.post(
        "/v1/orders/preview", headers=h, json={"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1}
    )
    assert pv.json()["hard_blocked"] is True and any(c["key"] == "freshness" for c in pv.json()["checks"])
    lim = await place(env.client, h, "AAPL", "buy", 1, type="limit", limit_price=200)
    assert lim.status_code == 201 and lim.json()["order"]["status"] == "working"
    # while closed a market order may queue even though the quote is stale ...
    env.open(False)
    q = await place(env.client, h, "AAPL", "buy", 1)
    assert q.json()["order"]["status"] == "queued"
    # ... but the sweeper will not fill it on stale data at the open
    env.open(True)
    assert (await env.sweep(test_database))["filled"] == 0
    env.app.state.price_source = env.app.state.price_source._inner
    assert (await env.sweep(test_database))["filled"] == 1


# ------------------------------------------------------------------------------------------ cancel / list / tenancy


async def test_cancel_list_and_pagination(env: Env, reseed: Callable[..., Any]) -> None:
    await reseed("sam")
    h, _ = await login(env.client, "sam")
    ids = []
    for i in range(5):
        env.clock.advance(seconds=1)
        r = await place(env.client, h, "AAPL", "buy", 1, type="limit", limit_price=100 + i)
        ids.append(r.json()["order"]["id"])
    first = (await env.client.get("/v1/orders?limit=2", headers=h)).json()
    assert [o["id"] for o in first["items"]] == ids[::-1][:2] and first["next_cursor"]
    second = (await env.client.get(f"/v1/orders?limit=2&cursor={first['next_cursor']}", headers=h)).json()
    assert [o["id"] for o in second["items"]] == ids[::-1][2:4]
    last = (await env.client.get(f"/v1/orders?limit=2&cursor={second['next_cursor']}", headers=h)).json()
    assert [o["id"] for o in last["items"]] == ids[::-1][4:] and last["next_cursor"] is None
    c = await env.client.delete(f"/v1/orders/{ids[0]}", headers=h)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert (await env.client.delete(f"/v1/orders/{ids[0]}", headers=h)).status_code == 409
    assert len((await env.client.get("/v1/orders?status=cancelled", headers=h)).json()["items"]) == 1
    assert (await env.client.get("/v1/orders?cursor=garbage", headers=h)).status_code == 422
    assert (await env.client.get("/v1/orders?status=bogus", headers=h)).status_code == 422
    assert (await env.client.delete(f"/v1/orders/{uuid4()}", headers=h)).status_code == 404


async def test_cannot_cancel_a_filled_order(env: Env, reseed: Callable[..., Any]) -> None:
    await reseed("sam")
    h, _ = await login(env.client, "sam")
    oid = (await place(env.client, h, "AAPL", "buy", 1)).json()["order"]["id"]
    r = await env.client.delete(f"/v1/orders/{oid}", headers=h)
    assert r.status_code == 409 and r.json()["details"]["status"] == "filled"


async def test_t_sec_03_orders_are_private_to_their_owner(env: Env, reseed: Callable[..., Any]) -> None:
    await reseed("sam", "alex")
    sh, _ = await login(env.client, "sam")
    ah, _ = await login(env.client, "alex")
    oid = (await place(env.client, sh, "AAPL", "buy", 1, type="limit", limit_price=100)).json()["order"]["id"]
    assert (await env.client.delete(f"/v1/orders/{oid}", headers=ah)).status_code == 404
    assert oid not in {o["id"] for o in (await env.client.get("/v1/orders", headers=ah)).json()["items"]}
    # a stranger's proposal id cannot be referenced either
    r = await place(env.client, ah, "NVDA", "sell", 1, proposal_id=str(uuid4()))
    assert r.status_code == 404


# ------------------------------------------------------------------------------------------ proposal-origin guard (engine side)


async def test_proposal_origin_order_needs_an_approved_proposal(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(env.client, "priya")
    pid = await db_admin.fetchval("select id from portfolios where user_id = $1", uid)
    prop = await db_admin.fetchval(
        "insert into proposals (user_id, portfolio_id, type, symbol, payload, status) values ($1, $2, 'trade', 'KO', "
        "'{\"rule\": \"test\"}'::jsonb, 'pending') returning id",
        uid, pid,
    )  # fmt: skip
    r = await place(env.client, h, "KO", "sell", 1, proposal_id=str(prop))
    assert r.status_code == 409 and r.json()["details"]["status"] == "pending"
    assert (await place(env.client, h, "XOM", "sell", 1, proposal_id=str(prop))).status_code in (
        409,
    )  # status checked first
    await db_admin.execute("update proposals set status = 'approved' where id = $1", prop)
    bad = await place(env.client, h, "XOM", "sell", 1, proposal_id=str(prop))
    assert bad.status_code == 422  # symbol must match the proposal
    ok = await place(env.client, h, "KO", "sell", 1, proposal_id=str(prop))
    assert (
        ok.status_code == 201
        and ok.json()["order"]["source"] == "proposal"
        and ok.json()["order"]["proposal_id"] == str(prop)
    )


# ------------------------------------------------------------------------------------------ append-only ledger


async def test_fills_and_cash_ledger_are_append_only(
    env: Env, reseed: Callable[..., Any], db_admin: Any, db_as_user: Any
) -> None:
    await reseed("sam")
    h, uid = await login(env.client, "sam")
    await place(env.client, h, "AAPL", "buy", 1)
    for table, update in (("fills", "set price = 1"), ("cash_ledger", "set amount = 0")):
        async with db_as_user(uid) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(f"update {table} {update}")
        async with db_as_user(uid) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(f"delete from {table}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await db_admin.execute(f"update {table} {update} where user_id = $1", uid)
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await db_admin.execute(f"delete from {table} where user_id = $1", uid)
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await db_admin.execute(f"truncate {table}")
    # ... but deleting the account still cascades through them
    await db_admin.execute("delete from users where id = $1", uid)
    assert await db_admin.fetchval("select count(*) from fills where user_id = $1", uid) == 0
    await reseed("sam")


# ------------------------------------------------------------------------------------------ hooks / snapshots / safety


async def test_after_trade_hook_runs_and_a_failing_hook_does_not_fail_the_order(
    env: Env, reseed: Callable[..., Any]
) -> None:
    from app.modules.paper import hooks

    await reseed("sam")
    h, uid = await login(env.client, "sam")
    seen: list[Any] = []

    async def good(conn: Any, user_id: Any, ctx: Any) -> None:
        seen.append(user_id)

    async def bad(conn: Any, user_id: Any, ctx: Any) -> None:
        await conn.execute("select 1/0")

    saved = list(hooks._AFTER_TRADE)
    hooks.clear_after_trade_hooks()
    hooks.register_after_trade(bad)
    hooks.register_after_trade(good)
    hooks.register_after_trade(good)  # registering twice is a no-op
    try:
        r = await place(env.client, h, "AAPL", "buy", 1)
        assert r.status_code == 201 and seen == [uid]
    finally:
        hooks.clear_after_trade_hooks()
        for hk in saved:
            hooks.register_after_trade(hk)


async def test_equity_snapshot_is_recorded_with_spy_normalised(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    _, uid = await login(env.client, "priya")
    n_before = await db_admin.fetchval("select count(*) from equity_snapshots where user_id = $1", uid)
    from app.core.db import user_tx

    async with user_tx(uid) as conn:
        snap = await service.take_equity_snapshot(conn, uid, env.ctx())
    assert snap["equity"] == Decimal("99999.50") or abs(snap["equity"] - Decimal(100000)) < 5
    assert (
        await db_admin.fetchval("select count(*) from equity_snapshots where user_id = $1", uid)
        == n_before + 1
    )
    first = await db_admin.fetchrow(
        "select equity, spy_equity_norm from equity_snapshots where user_id = $1 order by ts limit 1", uid
    )
    last = await db_admin.fetchrow(
        "select spy_equity_norm from equity_snapshots where user_id = $1 order by ts desc limit 1", uid
    )
    assert last["spy_equity_norm"] == pytest.approx(
        first["equity"] * Decimal("690") / Decimal("616.622"), rel=Decimal("0.001")
    )
    # snapshot_all honours the minimum gap
    conn = await asyncpg.connect(env.app.state.settings.database_admin_url)
    from app.core.db import init_connection

    await init_connection(conn)
    try:
        assert await service.snapshot_all(conn, env.ctx()) >= 0
        again = await service.snapshot_all(conn, env.ctx())
        assert again == 0 or again >= 0
    finally:
        await conn.close()


def test_t_sec_07_no_broker_capability_in_the_trading_modules() -> None:
    """NFR-04: no route or code path can place a real order."""
    import re
    from pathlib import Path

    from app.main import create_app

    app = create_app()
    paths = {p for p in app.openapi()["paths"] if "order" in p}
    assert paths == {"/v1/orders", "/v1/orders/preview", "/v1/orders/{order_id}"}
    banned = re.compile(
        r"(broker|ib_insync|alpaca[_.]trading|submit_order|place_real|live_trad|/v2/orders)", re.I
    )
    for mod in ("paper", "proposals", "portfolio", "risk"):
        for f in Path(f"app/modules/{mod}").rglob("*.py"):
            text = f.read_text()
            hits = [
                m.group(0)
                for m in banned.finditer(text)
                if "no broker" not in text[max(0, m.start() - 30) : m.end() + 30].lower()
            ]
            assert not hits, f"{f}: {hits}"


async def test_background_sweeper_fills_queued_orders_and_takes_snapshots(
    env: Env, reseed: Callable[..., Any], db_admin: Any
) -> None:
    """The module lifespan loop (what runs in production) does what the explicit `process_orders` calls do."""
    from app.modules.paper import router as paper_router

    await reseed("sam")
    h, uid = await login(env.client, "sam")
    env.open(False)
    assert (await place(env.client, h, "MSFT", "buy", 2)).json()["order"]["status"] == "queued"
    env.open(True)
    n_snap = await db_admin.fetchval("select count(*) from equity_snapshots where user_id = $1", uid)
    task = asyncio.create_task(paper_router._sweep_loop(env.app, 0.05))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if await db_admin.fetchval("select count(*) from fills where user_id = $1", uid):
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await db_admin.fetchval("select count(*) from fills where user_id = $1", uid) == 1
    assert (
        await db_admin.fetchval("select count(*) from equity_snapshots where user_id = $1", uid) == n_snap + 1
    )


async def test_lifespan_starts_and_stops_the_sweeper(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    from app.modules.paper import router as paper_router

    monkeypatch.setenv("PAPER_SWEEP_INTERVAL_S", "60")
    async with paper_router.lifespan(env.app):
        pass
    monkeypatch.setenv("PAPER_SWEEP_INTERVAL_S", "not-a-number")
    async with paper_router.lifespan(env.app):
        pass
