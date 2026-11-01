"""BE-04: `make seed` - universe, simulated prices, personas built from real fills; idempotent and deterministic."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.core.personas import PERSONAS
from scripts.seed import seed
from scripts.seed_data import ALEX, BENCHMARK, PERSONA_SEEDS, PRIYA, SAM, UNIVERSE
from tests.trading.support import login

pytestmark = pytest.mark.db


async def test_seed_runs_twice_without_error_and_keeps_persona_rows(
    seed_now: datetime, db_admin: Any
) -> None:
    before = {
        t: await db_admin.fetchval(f"select count(*) from {t}")
        for t in (
            "tickers",
            "prices_daily",
            "fills",
            "cash_ledger",
            "positions",
            "memories",
            "events",
            "proposals",
        )
    }
    report = await seed(db_admin, now=seed_now)  # second (and third) run: no reset
    again = await seed(db_admin, now=seed_now)
    assert report.personas == again.personas == {"priya": "skipped", "alex": "skipped", "sam": "skipped"}
    after = {t: await db_admin.fetchval(f"select count(*) from {t}") for t in before}
    assert after == before


async def test_universe_has_17_large_caps_in_7_sectors_with_ciks(db_admin: Any) -> None:
    rows = {r["symbol"]: r for r in await db_admin.fetch("select * from tickers where in_universe")}
    assert len(UNIVERSE) == 17 and {t.symbol for t in UNIVERSE} <= set(rows)  # (other tests may add tickers)
    seeded = [rows[t.symbol] for t in UNIVERSE]
    assert len({r["sector"] for r in seeded}) == 7
    assert all(r["cik"] and len(r["cik"]) == 10 and r["industry"] and r["exchange"] for r in seeded)
    assert rows["NVDA"]["cik"] == "0001045810"
    spy = await db_admin.fetchrow("select * from tickers where symbol = 'SPY'")
    assert spy["in_universe"] is False


async def test_prices_are_simulated_anchored_to_the_design_and_deterministic(
    db_admin: Any, seed_now: datetime
) -> None:
    for t in (*UNIVERSE, BENCHMARK):
        rows = await db_admin.fetch(
            "select date, close, source from prices_daily where symbol = $1 order by date", t.symbol
        )
        assert len(rows) == 120 and {r["source"] for r in rows} == {"simulated"}
        assert rows[-1]["close"] == t.px  # last close == design price
        change = (rows[-1]["close"] / rows[-2]["close"] - 1) * 100
        assert float(change) == pytest.approx(float(t.ch), abs=0.01)
    before = await db_admin.fetch("select symbol, date, close from prices_daily order by symbol, date")
    await seed(db_admin, now=seed_now, reset=True, personas=["sam"])  # regenerates the price rows
    assert (
        await db_admin.fetch("select symbol, date, close from prices_daily order by symbol, date") == before
    )


async def test_vendor_price_rows_are_never_touched_by_the_seed(db_admin: Any, seed_now: datetime) -> None:
    await db_admin.execute(
        "insert into prices_daily (symbol, date, close, source) values ('AAPL', '2001-01-02', 1.5, 'alpaca') on conflict do nothing"
    )
    try:
        await seed(db_admin, now=seed_now, reset=True, personas=["sam"])
        assert await db_admin.fetchval(
            "select close from prices_daily where symbol = 'AAPL' and date = '2001-01-02'"
        ) == Decimal("1.5")
    finally:
        await db_admin.execute("delete from prices_daily where date = '2001-01-02'")


@pytest.mark.parametrize("seed_p", [PRIYA, ALEX, SAM], ids=lambda p: p.key)
async def test_personas_match_the_design_exactly(db_admin: Any, seed_p: Any) -> None:
    uid = PERSONAS[seed_p.key].user_id
    pos = {
        r["symbol"]: (r["qty"], r["avg_cost"])
        for r in await db_admin.fetch("select symbol, qty, avg_cost from positions where user_id = $1", uid)
    }
    assert pos == {
        s: (Decimal(q), c) for s, q, c in seed_p.holdings
    }  # holdings AND cost basis, from real fills
    cash = await db_admin.fetchval("select sum(amount) from cash_ledger where user_id = $1", uid)
    assert cash == seed_p.cash
    rules = {
        r["rule_key"]: r["value"]
        for r in await db_admin.fetch("select rule_key, value from risk_rules where user_id = $1", uid)
    }
    assert rules == {k: Decimal(v) for k, v in seed_p.rules.items()}
    wl = [
        r["symbol"]
        for r in await db_admin.fetch(
            "select symbol from watchlist_items where user_id = $1 order by added_at", uid
        )
    ]
    assert wl == list(seed_p.watchlist)
    prof = await db_admin.fetchrow("select * from profiles where user_id = $1", uid)
    assert prof["risk_tier_stated"] == seed_p.tier and prof["disclaimer_ack_at"] is not None
    assert await db_admin.fetchval("select count(*) from memories where user_id = $1", uid) == len(
        seed_p.memories
    )
    arms = {
        r["arm"]: (int(r["alpha"]), int(r["beta"]))
        for r in await db_admin.fetch("select arm, alpha, beta from bandit_arms where user_id = $1", uid)
    }
    assert arms == seed_p.arms
    interest = {
        r["dim"]: float(r["weight"])
        for r in await db_admin.fetch("select dim, weight from user_interest_vector where user_id = $1", uid)
    }
    assert len(interest) == len(seed_p.interest) and max(interest.values()) == max(
        w for _, w in seed_p.interest
    )
    n_orders = await db_admin.fetchval(
        "select count(*) from orders where user_id = $1 and source = 'seed'", uid
    )
    assert n_orders == len(seed_p.orders)  # the design's recent orders, as real fills
    assert await db_admin.fetchval("select count(*) from alerts where user_id = $1", uid) == len(
        seed_p.alerts
    )
    assert await db_admin.fetchval(
        "select count(*) from events where user_id = $1 and type = 'order_filled'", uid
    ) >= len(seed_p.orders)
    assert await db_admin.fetchval("select count(*) from equity_snapshots where user_id = $1", uid) == 120


async def test_sam_is_balanced_with_eight_watchlist_names_and_no_positions(db_admin: Any) -> None:
    uid = PERSONAS["sam"].user_id
    assert SAM.tier == 3 and len(SAM.watchlist) == 8
    assert await db_admin.fetchval("select count(*) from positions where user_id = $1", uid) == 0
    assert await db_admin.fetchval("select sum(amount) from cash_ledger where user_id = $1", uid) == Decimal(
        100000
    )


async def test_dev_login_as_priya_and_alex_yields_the_designs_positions(tclient: httpx.AsyncClient) -> None:
    for who, n in (("priya", 9), ("alex", 6)):
        h, _ = await login(tclient, who)
        me = (await tclient.get("/v1/me", headers=h)).json()
        assert me["onboarded"] is True and me["persona_id"] == who
        assert len((await tclient.get("/v1/portfolio", headers=h)).json()["positions"]) == n


async def test_ledger_reconciles_cash_plus_positions_equals_equity(db_admin: Any) -> None:
    from app.modules.paper import service

    for key in PERSONA_SEEDS:
        pid = await db_admin.fetchval("select id from portfolios where user_id = $1", PERSONAS[key].user_id)
        rec = await service.reconcile(db_admin, pid)
        assert rec["ok"] and rec["position_mismatches"] == []
        snap = await db_admin.fetchval(
            "select equity from equity_snapshots where portfolio_id = $1 order by ts desc limit 1", pid
        )
        assert snap == pytest.approx(
            rec["equity"], abs=Decimal("0.01")
        )  # last snapshot == cash + sum(qty x last)


async def test_reset_rebuilds_and_unknown_persona_is_an_error(db_admin: Any, seed_now: datetime) -> None:
    uid = PERSONAS["priya"].user_id
    await db_admin.execute(
        "insert into watchlist_items (user_id, symbol) values ($1, 'NVDA') on conflict do nothing", uid
    )
    report = await seed(db_admin, now=seed_now, reset=True, personas=["priya"])
    assert report.personas == {"priya": "seeded"} and report.proposals["priya"] == 3
    assert (
        await db_admin.fetchval(
            "select count(*) from watchlist_items where user_id = $1 and symbol = 'NVDA'", uid
        )
        == 0
    )
    with pytest.raises(ValueError, match="unknown persona"):
        await seed(db_admin, now=seed_now, personas=["nobody"])


async def test_persona_with_leftover_rows_but_no_portfolio_is_rebuilt_not_crashed(
    db_admin: Any, seed_now: datetime
) -> None:
    """Regression (found by running `make seed` on the dev DB): dev-login / background jobs can leave rows for a
    persona that has no portfolio yet; the seed must treat that persona as not seeded and start clean."""
    uid = PERSONAS["alex"].user_id
    await db_admin.execute(
        "delete from portfolios where user_id = $1", uid
    )  # leaves interest vector, memories, ...
    assert await db_admin.fetchval("select count(*) from user_interest_vector where user_id = $1", uid) > 0
    report = await seed(db_admin, now=seed_now, personas=["alex"])
    assert report.personas == {"alex": "seeded"}
    assert await db_admin.fetchval("select count(*) from positions where user_id = $1", uid) == 6
