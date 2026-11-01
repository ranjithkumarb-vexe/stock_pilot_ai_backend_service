"""Portfolio / risk / performance read models built from the ledger, quotes and the pure risk engine."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.modules.paper.context import EngineContext
from app.modules.paper.ledger import FillRecord, realised_pnl_of
from app.modules.paper.pricing import quotes_for
from app.modules.paper.state import PortfolioState, load_state
from app.modules.risk import compute_metrics, current_violations, curve_summary, rule_statuses

ZERO = Decimal(0)
HUNDRED = Decimal(100)
MAX_CURVE_POINTS = 400


def r2(x: Decimal | float | None) -> float | None:
    if x is None:
        return None
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def r4(x: Decimal | float | None) -> float | None:
    if x is None:
        return None
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def portfolio_view(state: PortfolioState, ctx: EngineContext) -> dict[str, Any]:
    """CONTRACT portfolio. Weights are % of EQUITY (cash included); HHI uses invested weights (design `calc`)."""
    equity, invested = state.equity, state.invested
    positions = []
    for p in sorted(state.positions, key=lambda x: (-x.value, x.symbol)):
        cost = p.qty * p.avg_cost
        positions.append(
            {
                "symbol": p.symbol, "name": p.name, "sector": p.sector, "qty": int(p.qty) if p.qty == p.qty.to_integral_value() else float(p.qty), "avg_cost": r4(p.avg_cost),
                "price": r4(p.price), "change_pct": r2(p.change_pct), "value": r2(p.value),
                "weight_pct": r2(p.value / equity * HUNDRED) if equity > 0 else 0.0,
                "pnl": r2(p.value - cost), "pnl_pct": r2((p.price / p.avg_cost - 1) * HUNDRED) if p.avg_cost > 0 else 0.0,
            }
        )  # fmt: skip
    metrics = compute_metrics(state.to_risk())
    sectors = [
        {"sector": s, "value": r2(sum((p.value for p in state.positions if (p.sector or "Unclassified") == s), ZERO)), "weight_pct": r2(w * HUNDRED)}
        for s, w in sorted(metrics.sector_weights.items(), key=lambda kv: (-kv[1], kv[0]))
    ]  # fmt: skip
    day = metrics.day_change
    return {
        "portfolio_id": state.portfolio_id,
        "cash": r2(state.cash), "invested": r2(invested), "equity": r2(equity),
        "day_change": r2(day), "day_change_pct": r2(day / equity * HUNDRED) if equity > 0 else 0.0,
        "positions": positions, "sectors": sectors,
        "hhi": r4(metrics.hhi) if metrics.hhi is not None else 0.0,
        "effective_n": r2(metrics.effective_n) if metrics.effective_n is not None else 0.0,
        "as_of": iso(ctx.clock.now()),
        "price_source": next((p.quote.source for p in state.positions if p.quote is not None), None),
    }  # fmt: skip


async def get_portfolio(conn: Connection, user_id: UUID, ctx: EngineContext) -> dict[str, Any]:
    return portfolio_view(await load_state(conn, user_id, ctx), ctx)


async def _curves(
    conn: Connection, state: PortfolioState, ctx: EngineContext, limit: int | None = None
) -> tuple[list[datetime], list[Decimal], list[Decimal | None]]:
    rows = await conn.fetch(
        "select ts, equity, spy_equity_norm from equity_snapshots where portfolio_id = $1 order by ts desc limit $2",
        state.portfolio_id,
        limit or 100_000,
    )
    rows = list(reversed(rows))
    ts = [r["ts"] for r in rows]
    eq = [Decimal(r["equity"]) for r in rows]
    spy: list[Decimal | None] = [
        None if r["spy_equity_norm"] is None else Decimal(r["spy_equity_norm"]) for r in rows
    ]
    # extend with the live point when it moved (a real intraday change, not rounding noise)
    now = ctx.clock.now()
    if rows and now > ts[-1] and abs(state.equity - eq[-1]) >= Decimal("0.01"):
        spy_q = (await quotes_for(ctx.prices, ["SPY"], conn)).get("SPY")
        base = await conn.fetchval(
            "select close from prices_daily where symbol = 'SPY' and date <= $1 order by date desc limit 1",
            ts[0].date(),
        )
        if spy_q is not None and base and eq[0] > 0:
            ts.append(now)
            eq.append(state.equity)
            spy.append(eq[0] * spy_q.price / Decimal(base))
    return ts, eq, spy


def _series_ok(spy: list[Decimal | None]) -> list[Decimal] | None:
    return [s for s in spy if s is not None] if spy and all(s is not None for s in spy) else None


async def get_risk(conn: Connection, user_id: UUID, ctx: EngineContext) -> dict[str, Any]:
    state = await load_state(conn, user_id, ctx)
    _, eq, spy = await _curves(conn, state, ctx, 253)
    bench = _series_ok(spy)
    metrics = compute_metrics(
        state.to_risk(),
        equity_curve=eq or None,
        benchmark_curve=bench if bench and len(bench) == len(eq) else None,
    )
    return {
        "metrics": {
            "hhi": r4(metrics.hhi), "effective_n": r2(metrics.effective_n), "vol_60d": r2(metrics.vol_60d),
            "beta": r2(metrics.beta), "max_drawdown_pct": r2(metrics.max_drawdown_pct),
            "drawdown_pct": r2(metrics.drawdown_pct), "trades_7d": metrics.trades_7d,
        },
        "rules": [s.to_dict() for s in rule_statuses(metrics, state.rules)],
        "violations": [v.to_dict() for v in current_violations(metrics, state.rules)],
        "flags": list(metrics.flags),
    }  # fmt: skip


async def get_performance(
    conn: Connection, user_id: UUID, ctx: EngineContext, days: int | None = None
) -> dict[str, Any]:
    state = await load_state(conn, user_id, ctx)
    ts, eq, spy = await _curves(conn, state, ctx)
    if days:
        cutoff = ctx.clock.now() - timedelta(days=days)
        keep = [i for i, t in enumerate(ts) if t >= cutoff]
        ts, eq, spy = [ts[i] for i in keep], [eq[i] for i in keep], [spy[i] for i in keep]
    if len(ts) > MAX_CURVE_POINTS:  # thin evenly, always keep the last point
        step = -(-len(ts) // MAX_CURVE_POINTS)
        idx = sorted(set(range(0, len(ts), step)) | {len(ts) - 1})
        ts, eq, spy = [ts[i] for i in idx], [eq[i] for i in idx], [spy[i] for i in idx]
    bench = _series_ok(spy)
    summ = curve_summary(eq, bench if bench and len(bench) == len(eq) else None)
    fills = await conn.fetch(
        "select f.symbol, o.side, f.qty, f.price, f.filled_at, f.fee from fills f join orders o on o.id = f.order_id "
        "where f.user_id = $1 order by f.filled_at, f.seq",
        user_id,
    )
    records = [
        FillRecord(
            f["symbol"], f["side"], Decimal(f["qty"]), Decimal(f["price"]), f["filled_at"], Decimal(f["fee"])
        )
        for f in fills
    ]
    sells = realised_pnl_of(records)
    realised = sum((p for _, p in sells), ZERO)
    wins = sum(1 for _, p in sells if p > 0)
    turnover = None
    if records and eq:
        span_days = max(30.0, (ctx.clock.now() - records[0].filled_at).total_seconds() / 86400)
        window = min(365.0, span_days)
        cutoff = ctx.clock.now() - timedelta(days=window)
        sold = sum((f.qty * f.price for f in records if f.side == "sell" and f.filled_at > cutoff), ZERO)
        avg_eq = sum(eq, ZERO) / len(eq)
        turnover = float(sold / avg_eq * Decimal(365) / Decimal(window) * HUNDRED) if avg_eq > 0 else None
    return {
        "series": [{"ts": iso(t), "equity": r2(e), "spy_norm": r2(s)} for t, e, s in zip(ts, eq, spy, strict=True)],
        "stats": {
            "ytd_pct": r2(summ.period_return_pct), "spy_pct": r2(summ.benchmark_return_pct), "vol_pct": r2(summ.vol_pct),
            "beta": r2(summ.beta), "drawdown_pct": r2(summ.drawdown_pct), "max_drawdown_pct": r2(summ.max_drawdown_pct),
            "turnover_pct": r2(turnover), "win_rate_pct": r2(wins / len(sells) * 100) if sells else None,
            "realized_pnl": r2(realised),
        },
        "window": {"start": iso(ts[0]) if ts else None, "end": iso(ts[-1]) if ts else None, "points": len(ts)},
        "flags": list(summ.flags),
    }  # fmt: skip
