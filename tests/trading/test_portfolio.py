"""BE-08: portfolio / risk / performance APIs; seeded Priya and Alex must equal the design's own `calc()`
(golden file produced by running the design's JS with `scripts/design_parity.mjs`)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.trading.support import login, place

pytestmark = pytest.mark.db
GOLDEN = json.loads((Path(__file__).parent / "golden" / "design_parity.json").read_text())


@pytest.mark.parametrize("persona", ["priya", "alex"])
async def test_portfolio_equals_design_calc(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], persona: str
) -> None:
    await reseed(persona)
    h, _ = await login(tclient, persona)
    r = await tclient.get("/v1/portfolio", headers=h)
    assert r.status_code == 200, r.text
    j = r.json()
    g = GOLDEN["personas"][persona]
    assert j["equity"] == pytest.approx(g["T"], abs=0.01) and j["invested"] == pytest.approx(
        g["inv"], abs=0.01
    )
    assert j["cash"] == pytest.approx(g["T"] - g["inv"], abs=0.01)
    assert j["day_change"] == pytest.approx(g["day"], abs=0.01) and j["day_change_pct"] == pytest.approx(
        g["dayPct"], abs=0.01
    )
    assert j["hhi"] == pytest.approx(g["hhi"], abs=1e-4) and j["effective_n"] == pytest.approx(
        g["eff"], abs=0.01
    )
    by_symbol = {p["symbol"]: p for p in j["positions"]}
    assert set(by_symbol) == {p["t"] for p in g["pos"]}
    for gp in g["pos"]:
        p = by_symbol[gp["t"]]
        assert p["qty"] == gp["q"] and p["price"] == gp["px"] and p["avg_cost"] == gp["c"]
        assert p["value"] == pytest.approx(gp["v"], abs=0.01) and p["weight_pct"] == pytest.approx(
            gp["w"], abs=0.01
        )
        assert p["pnl"] == pytest.approx(gp["pnl"], abs=0.01) and p["pnl_pct"] == pytest.approx(
            gp["pp"], abs=0.01
        )
        assert p["change_pct"] == pytest.approx(gp["ch"], abs=0.01) and p["sector"] == gp["s"]
    assert [s["sector"] for s in j["sectors"]] == [s["s"] for s in g["sec"]]
    for s, gs in zip(j["sectors"], g["sec"], strict=True):
        assert s["value"] == pytest.approx(gs["v"], abs=0.01) and s["weight_pct"] == pytest.approx(
            gs["w"], abs=0.01
        )
    assert {
        "portfolio_id",
        "positions",
        "sectors",
        "hhi",
        "effective_n",
        "day_change",
        "day_change_pct",
    } <= set(j)
    assert set(j["positions"][0]) == {
        "symbol",
        "name",
        "sector",
        "qty",
        "avg_cost",
        "price",
        "change_pct",
        "value",
        "weight_pct",
        "pnl",
        "pnl_pct",
    }


async def test_seeded_position_counts_match_the_brief(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya", "alex", "sam")
    counts = {}
    for who in ("priya", "alex", "sam"):
        h, _ = await login(tclient, who)
        counts[who] = len((await tclient.get("/v1/portfolio", headers=h)).json()["positions"])
    assert counts == {"priya": 9, "alex": 6, "sam": 0}
    h, _ = await login(tclient, "sam")
    sam = (await tclient.get("/v1/portfolio", headers=h)).json()
    assert sam["cash"] == 100000 and sam["equity"] == 100000 and sam["hhi"] == 0 and sam["sectors"] == []


async def test_ledger_reconciles_for_every_persona(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    from app.modules.paper import service

    await reseed("priya", "alex", "sam")
    for who in ("priya", "alex", "sam"):
        h, uid = await login(tclient, who)
        pid = await db_admin.fetchval("select id from portfolios where user_id = $1", uid)
        rec = await service.reconcile(db_admin, pid)
        assert rec["ok"], (who, rec)
        j = (await tclient.get("/v1/portfolio", headers=h)).json()
        # cash + sum(qty x last) = equity
        assert j["cash"] + sum(p["qty"] * p["price"] for p in j["positions"]) == pytest.approx(
            j["equity"], abs=0.02
        )
        assert float(rec["equity"]) == pytest.approx(j["equity"], abs=0.01)


@pytest.mark.parametrize("persona", ["priya", "alex"])
async def test_orders_preview_checks_equal_the_designs_check(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], persona: str
) -> None:
    await reseed(persona)
    h, _ = await login(tclient, persona)
    marks = {"pass": "Pass", "note": "Note", "blocked": "Blocked"}
    scenarios = [c for c in GOLDEN["checks"] if c["pid"] == persona]
    assert scenarios
    for sc in scenarios:
        r = await tclient.post(
            "/v1/orders/preview",
            headers=h,
            json={"symbol": sc["symbol"], "side": sc["side"], "type": "market", "qty": sc["qty"]},
        )
        assert r.status_code == 200, (sc, r.text)
        got = [(c["label"], c["detail"], marks[c["severity"]]) for c in r.json()["checks"]]
        want = [(row["l"], row["d"], row["mark"]) for row in sc["rows"]]
        if sc["symbol"] in {"PG", "PEP"} and sc["qty"] >= 100:
            # the engine additionally enforces the slippage buffer on cash; compare the design rows that exist
            assert [g for g in got if g[0] != "Paper cash"] == [w for w in want if w[0] != "Paper cash"], sc
            continue
        assert got == want, sc
        assert r.json()["blocked"] == sc["blocked"], sc


@pytest.mark.parametrize("persona", ["priya", "alex"])
async def test_risk_endpoint_flags_the_designs_breaches(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], persona: str
) -> None:
    await reseed(persona)
    h, _ = await login(tclient, persona)
    r = await tclient.get("/v1/portfolio/risk", headers=h)
    assert r.status_code == 200, r.text
    j = r.json()
    g = GOLDEN["personas"][persona]
    assert set(j["metrics"]) == {
        "hhi",
        "effective_n",
        "vol_60d",
        "beta",
        "max_drawdown_pct",
        "drawdown_pct",
        "trades_7d",
    }
    assert j["metrics"]["hhi"] == pytest.approx(g["hhi"], abs=1e-4)
    rules = {x["key"]: x for x in j["rules"]}
    assert set(rules) == {"pos", "sec", "dd", "trades", "cash"} and {
        "key",
        "limit",
        "current",
        "status",
    } <= set(rules["pos"])
    assert rules["pos"]["status"] == "breach" and rules["sec"]["status"] == "breach"
    top = max(g["pos"], key=lambda p: p["w"])
    assert rules["pos"]["subject"] == top["t"] and rules["pos"]["current"] == pytest.approx(
        top["w"], abs=0.01
    )
    assert rules["sec"]["current"] == pytest.approx(g["sec"][0]["w"], abs=0.01)
    assert {v["rule"] for v in j["violations"]} >= {"pos", "sec"}
    # design stats (calibrated by the seed): beta over the full window, drawdown from the equity curve
    expected = {
        "priya": {"trades": 1, "dd": 3.1, "beta": 0.62, "state": "ok"},
        "alex": {"trades": 9, "dd": 18.3, "beta": 1.74, "state": "warn"},
    }[persona]
    assert j["metrics"]["trades_7d"] == expected["trades"] and rules["trades"]["status"] == expected["state"]
    assert j["metrics"]["drawdown_pct"] == pytest.approx(expected["dd"], abs=0.3)
    assert j["metrics"]["beta"] == pytest.approx(expected["beta"], abs=0.08)  # 60-252 d window ~ the full 119
    assert j["metrics"]["vol_60d"] is not None and j["metrics"]["vol_60d"] > 0
    assert j["flags"] == []


@pytest.mark.parametrize("persona", ["priya", "alex"])
async def test_performance_series_and_stats_match_the_design_stats(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], persona: str
) -> None:
    await reseed(persona)
    h, _ = await login(tclient, persona)
    r = await tclient.get("/v1/portfolio/performance", headers=h)
    assert r.status_code == 200, r.text
    j = r.json()
    assert len(j["series"]) == 120 and {"ts", "equity", "spy_norm"} == set(j["series"][0])
    assert j["series"][0]["equity"] == pytest.approx(
        j["series"][0]["spy_norm"], abs=0.01
    )  # SPY normalised to the same start
    s = j["stats"]
    design = {
        "priya": {
            "ytd_pct": 6.8,
            "spy_pct": 11.9,
            "vol_pct": 11.4,
            "beta": 0.62,
            "drawdown_pct": 3.1,
            "max_drawdown_pct": 5.8,
        },
        "alex": {
            "ytd_pct": 24.5,
            "spy_pct": 11.9,
            "vol_pct": 48.3,
            "beta": 1.74,
            "drawdown_pct": 18.2,
            "max_drawdown_pct": 21.6,
        },
    }[persona]
    for k, v in design.items():
        assert s[k] == pytest.approx(v, abs=0.15), (k, s[k], v)
    assert set(s) == {
        "ytd_pct",
        "spy_pct",
        "vol_pct",
        "beta",
        "drawdown_pct",
        "max_drawdown_pct",
        "turnover_pct",
        "win_rate_pct",
        "realized_pnl",
    }
    assert j["series"][-1]["equity"] == pytest.approx(GOLDEN["personas"][persona]["T"], abs=0.01)
    # realised P&L comes from the fills (average-cost): Priya sold 8 JPM at 296.00 vs avg cost 248.30
    if persona == "priya":
        assert s["realized_pnl"] == pytest.approx(8 * (296.0 - 248.3), abs=0.01)
    assert (await tclient.get("/v1/portfolio/performance?days=30", headers=h)).json()["window"]["points"] < 30


async def test_new_portfolio_has_none_flagged_metrics_not_errors(tclient: httpx.AsyncClient) -> None:
    from tests.trading.test_onboarding import login as login_any
    from tests.trading.test_onboarding import onboarding_body

    h, _ = await login_any(tclient, "new")
    assert (await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())).status_code == 200
    risk = (await tclient.get("/v1/portfolio/risk", headers=h)).json()
    assert (
        risk["metrics"]["vol_60d"] is None
        and risk["metrics"]["beta"] is None
        and "insufficient_history" in risk["flags"]
    )
    assert risk["metrics"]["hhi"] is None and risk["violations"] == []
    perf = (await tclient.get("/v1/portfolio/performance", headers=h)).json()
    assert (
        len(perf["series"]) == 1 and perf["stats"]["vol_pct"] is None
    )  # equity curve non-empty from day one
    assert perf["stats"]["win_rate_pct"] is None and perf["stats"]["realized_pnl"] == 0


async def test_portfolio_moves_after_a_trade_and_uses_live_quotes(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    from decimal import Decimal

    from app.core.pricesource import Quote

    await reseed("sam")
    h, _ = await login(tclient, "sam")
    assert (await place(tclient, h, "AAPL", "buy", 10)).status_code == 201

    class Live:
        name = "live-test"

        async def get_last(self, symbol: str) -> Quote:
            ts = tclient.clock.now()  # type: ignore[attr-defined]
            return Quote(symbol, Decimal("300"), ts, "alpaca", prev_close=Decimal("290"), freshness="live")

        def subscribe(self, symbols: Any) -> Any:
            raise NotImplementedError

    tclient.app.state.price_source = Live()  # type: ignore[attr-defined]
    j = (await tclient.get("/v1/portfolio", headers=h)).json()
    p = j["positions"][0]
    assert (
        p["symbol"] == "AAPL"
        and p["price"] == 300
        and p["value"] == 3000
        and p["pnl"] == pytest.approx(3000 - 2688.3, abs=0.01)
    )
    assert j["day_change"] == 100 and j["price_source"] == "alpaca"
    perf = (await tclient.get("/v1/portfolio/performance", headers=h)).json()
    assert perf["series"][-1]["equity"] == pytest.approx(j["equity"], abs=0.01)  # live point appended
    assert len(perf["series"]) == 121


async def test_portfolio_requires_auth_and_is_private(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya", "alex")
    for path in ("/v1/portfolio", "/v1/portfolio/risk", "/v1/portfolio/performance"):
        assert (await tclient.get(path)).status_code == 401
    ph, _ = await login(tclient, "priya")
    ah, _ = await login(tclient, "alex")
    p = {x["symbol"] for x in (await tclient.get("/v1/portfolio", headers=ph)).json()["positions"]}
    a = {x["symbol"] for x in (await tclient.get("/v1/portfolio", headers=ah)).json()["positions"]}
    assert p.isdisjoint(a)
