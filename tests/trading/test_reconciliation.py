"""T-PAPER-04: after 1,000 random orders the ledger reconciles and positions are rebuildable from fills."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.core.pricesource import StaticPriceSource
from app.modules.paper import service
from app.modules.paper.context import EngineContext
from app.modules.paper.market import MarketState
from app.modules.paper.state import load_state

pytestmark = pytest.mark.db
SYMBOLS = ["AAPL", "MSFT", "KO", "JNJ", "XOM", "NVDA", "PG", "JPM"]
START = {
    "AAPL": "268.70",
    "MSFT": "521.60",
    "KO": "69.40",
    "JNJ": "171.80",
    "XOM": "114.20",
    "NVDA": "182.40",
    "PG": "156.30",
    "JPM": "301.50",
}


async def test_t_paper_04_ledger_reconciles_after_1000_random_orders(
    seed_now: Any, make_user: Any, db_admin: Any
) -> None:
    rng = random.Random(20261101)
    uid: UUID = await make_user()
    clock = FakeClock(start=seed_now)
    prices = StaticPriceSource({s: Decimal(p) for s, p in START.items()}, clock.now())
    state = {"open": True}
    ctx = EngineContext(
        clock=clock,
        prices=prices,
        market=lambda now: MarketState(state["open"], "regular" if state["open"] else "closed", None, None),
        slippage_bps=Decimal(5),
    )
    pid = await db_admin.fetchval(
        "insert into portfolios (user_id, name, starting_cash) values ($1, 'Main', 250000) returning id", uid
    )
    await db_admin.execute(
        "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount) values ($1, $2, $3, 'deposit', 250000)",
        pid, uid, clock.now(),
    )  # fmt: skip
    await db_admin.execute(
        "insert into risk_rules (user_id, rule_key, value) values ($1,'pos',60),($1,'sec',100),($1,'dd',50),($1,'trades',100000),($1,'cash',0)",
        uid,
    )
    await db_admin.execute(
        "update portfolios set fee_per_order = 0.25 where id = $1", pid
    )  # exercise fee rows too

    placed = replays = errors = 0
    keys: list[tuple[str, dict[str, Any]]] = []
    for _ in range(1000):
        roll = rng.random()
        clock.advance(seconds=30)
        if roll < 0.78:
            sym = rng.choice(SYMBOLS)
            side = "buy" if rng.random() < 0.58 else "sell"
            last = (await prices.get_last(sym)).price
            is_limit = rng.random() < 0.3
            spec: dict[str, Any] = {
                "symbol": sym, "side": side, "type": "limit" if is_limit else "market", "qty": Decimal(rng.randint(1, 60)),
                "limit_price": (last * Decimal(str(round(rng.uniform(0.96, 1.04), 4)))).quantize(Decimal("0.01")) if is_limit else None,
            }  # fmt: skip
            key = str(UUID(int=rng.getrandbits(128), version=4))
            intent = service.OrderIntent(
                spec["symbol"], spec["side"], spec["type"], spec["qty"], spec["limit_price"], override=True
            )
            try:
                async with db_admin.transaction():
                    res = await service.place_order(db_admin, uid, intent, key, ctx)
                placed += 1
                keys.append((key, spec))
                assert res.replay is False
            except AppError:
                errors += 1  # no cash / no shares / ... : must leave no trace
        elif roll < 0.88:
            for s in SYMBOLS:
                last = prices._prices[s]
                prices.set_price(
                    s,
                    (last * Decimal(str(round(rng.gauss(1, 0.012), 4)))).quantize(Decimal("0.01")),
                    clock.now(),
                )
            async with db_admin.transaction():
                await service.process_orders(db_admin, ctx)
        elif roll < 0.92:
            state["open"] = not state["open"]
            async with db_admin.transaction():
                await service.process_orders(db_admin, ctx)
        elif roll < 0.96:
            rows = await db_admin.fetch(
                "select id from orders where user_id = $1 and status in ('queued','working')", uid
            )
            if rows:
                async with db_admin.transaction():
                    await service.cancel_order(db_admin, uid, rng.choice(rows)["id"], ctx)
        elif keys:
            key, spec = rng.choice(keys)  # duplicate submission must replay, never double-fill
            intent = service.OrderIntent(
                spec["symbol"], spec["side"], spec["type"], spec["qty"], spec["limit_price"], override=True
            )
            async with db_admin.transaction():
                res = await service.place_order(db_admin, uid, intent, key, ctx)
            assert res.replay is True
            replays += 1

    state["open"] = True
    async with db_admin.transaction():
        await service.process_orders(db_admin, ctx)

    n_orders = await db_admin.fetchval("select count(*) from orders where user_id = $1", uid)
    n_fills = await db_admin.fetchval("select count(*) from fills where user_id = $1", uid)
    assert placed >= 500 and n_orders == placed and n_fills >= 300 and replays > 10

    # --- accounting invariants -------------------------------------------------------------
    last_px = {s: (await prices.get_last(s)).price for s in SYMBOLS}
    rec = await service.reconcile(db_admin, pid, prices=last_px)
    assert rec["ok"], rec
    assert rec["position_mismatches"] == []
    st = await load_state(db_admin, uid, ctx)
    assert st.cash == rec["cash"] >= 0
    assert (
        st.equity
        == rec["equity"]
        == st.cash + sum((p.qty * last_px[p.symbol] for p in st.positions), Decimal(0))
    )
    # cash from first principles: deposit - buys + sells - fees
    rows = await db_admin.fetch(
        "select o.side, f.qty, f.price, f.fee from fills f join orders o on o.id = f.order_id where f.user_id = $1",
        uid,
    )
    cash = Decimal(250000) + sum(
        (-(r["qty"] * r["price"]) if r["side"] == "buy" else r["qty"] * r["price"]) - r["fee"] for r in rows
    )
    assert cash == st.cash
    assert (
        await db_admin.fetchval("select count(*) from cash_ledger where user_id = $1 and kind = 'fee'", uid)
        > 0
    )
    # shares: net of fills == positions, never negative
    per_symbol = await db_admin.fetch(
        "select f.symbol, sum(case when o.side = 'buy' then f.qty else -f.qty end) as net from fills f join orders o on o.id = f.order_id where f.user_id = $1 group by f.symbol",
        uid,
    )
    held = {p.symbol: p.qty for p in st.positions}
    assert all(r["net"] >= 0 for r in per_symbol)
    assert {r["symbol"]: r["net"] for r in per_symbol if r["net"] > 0} == held
    # order bookkeeping: every filled order has fills summing to its qty; others have none
    for o in await db_admin.fetch(
        "select o.id, o.status, o.qty, coalesce(sum(f.qty), 0) as filled from orders o left join fills f on f.order_id = o.id where o.user_id = $1 group by o.id",
        uid,
    ):
        assert o["filled"] == (o["qty"] if o["status"] == "filled" else 0)
    assert errors > 0  # the generator really did hit rejections (no cash / no shares)
