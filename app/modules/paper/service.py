"""Paper trading on the ledger tables: place / preview / cancel / sweep orders, equity snapshots, splits,
reconciliation. All functions take an open connection inside the caller's transaction:

* request path   - `UserConn` (RLS, `app.user_id` set)
* jobs / seed    - an owner connection; every query filters by explicit user/portfolio ids

The portfolio row is locked (`for update`) by every mutating operation, which serialises order placement per
portfolio (no cash race, idempotency replays are strictly sequential).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.core.audit import write_audit
from app.core.db import Connection
from app.core.errors import (
    AppError,
    ErrorCode,
    conflict,
    not_found,
    upstream_unavailable,
    validation_error,
)
from app.core.logging import get_logger
from app.core.pricesource import Quote
from app.modules.paper import engine
from app.modules.paper.context import EngineContext
from app.modules.paper.hooks import run_after_trade
from app.modules.paper.ledger import (
    FillRecord,
    Lot,
    SplitRecord,
    apply_buy,
    apply_sell,
    cash_effect,
    rebuild_positions,
    split_effective_at,
)
from app.modules.paper.market import MarketState
from app.modules.paper.pricing import latest_closes, quotes_for
from app.modules.paper.state import PortfolioState, cash_balance, get_portfolio_row, load_state
from app.modules.risk import Portfolio, TradeProposal, validate
from app.modules.tradingcommon.events import emit_event, universe_row

log = get_logger("app.paper")
ZERO = Decimal(0)
RESTING = ("queued", "working")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    side: engine.Side
    type: engine.OrderType
    qty: Decimal
    limit_price: Decimal | None = None
    proposal_id: UUID | None = None
    override: bool = False
    source: str | None = None

    @property
    def spec(self) -> engine.OrderSpec:
        return engine.OrderSpec(self.symbol, self.side, self.type, self.qty, self.limit_price)


@dataclass(slots=True)
class Evaluation:
    state: PortfolioState
    quote: Quote
    market: MarketState
    checks: list[Any]
    summary: engine.CheckSummary
    risk: Any
    est_price: Decimal
    decision: engine.Decision
    ticker: dict[str, Any]


@dataclass(slots=True)
class PlaceResult:
    order: dict[str, Any]
    fill: dict[str, Any] | None
    checks: list[dict[str, Any]]
    blocked: bool
    replay: bool = False


# --------------------------------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------------------------------


def fill_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "order_id": row["order_id"], "symbol": row["symbol"], "qty": row["qty"],
        "price": row["price"], "slippage_bps": row["slippage_bps"], "fee": row["fee"], "filled_at": row["filled_at"],
    }  # fmt: skip


def order_dict(row: Any, fills: list[Any]) -> dict[str, Any]:
    filled_qty = sum((Decimal(f["qty"]) for f in fills), ZERO)
    avg = (
        (sum((Decimal(f["qty"]) * Decimal(f["price"]) for f in fills), ZERO) / filled_qty)
        if filled_qty
        else None
    )
    return {
        "id": row["id"], "symbol": row["symbol"], "side": row["side"], "type": row["type"], "qty": row["qty"],
        "limit_price": row["limit_price"], "tif": row["tif"], "status": row["status"], "source": row["source"],
        "proposal_id": row["proposal_id"], "override": row["override"], "reject_reason": row["reject_reason"],
        "filled_qty": filled_qty, "avg_fill_price": None if avg is None else avg.quantize(Decimal("0.0001")),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "expires_at": row["expires_at"],
    }  # fmt: skip


async def _fills_for(conn: Connection, order_ids: list[UUID]) -> dict[UUID, list[Any]]:
    if not order_ids:
        return {}
    out: dict[UUID, list[Any]] = {}
    for f in await conn.fetch(
        "select * from fills where order_id = any($1::uuid[]) order by filled_at, id", order_ids
    ):
        out.setdefault(f["order_id"], []).append(f)
    return out


async def load_order(conn: Connection, user_id: UUID, order_id: UUID) -> tuple[Any, list[Any]]:
    row = await conn.fetchrow("select * from orders where id = $1 and user_id = $2", order_id, user_id)
    if row is None:
        raise not_found("Order not found")
    return row, (await _fills_for(conn, [order_id])).get(order_id, [])


# --------------------------------------------------------------------------------------------------
# evaluation (shared by preview and place)
# --------------------------------------------------------------------------------------------------


async def evaluate(
    conn: Connection, user_id: UUID, intent: OrderIntent, ctx: EngineContext, *, lock: bool
) -> Evaluation:
    ticker = await universe_row(conn, intent.symbol)
    state = await load_state(conn, user_id, ctx, lock=lock)
    if ticker is None:
        raise AppError(
            ErrorCode.UNSUPPORTED_SYMBOL,
            f"{intent.symbol} is not in the supported universe",
            details={"symbol": intent.symbol},
        )
    quotes = await quotes_for(ctx.prices, [intent.symbol], conn)
    quote = quotes.get(intent.symbol)
    if quote is None:
        raise upstream_unavailable(f"No price available for {intent.symbol}")
    market = ctx.market_now()
    spec = intent.spec
    est = engine.estimated_price(spec, quote, ctx.slippage_bps)
    # design parity: weights/sector/cash-buffer are judged at the LAST price; funds also at worst-case + fee
    risk = validate(
        TradeProposal(intent.symbol, intent.side, intent.qty, quote.price, ticker["sector"]),
        Portfolio(state.cash, state.to_risk().positions, state.trades_7d),
        state.rules,
    )
    checks = engine.assemble_checks(
        risk, spec, quote, market.is_open, state.after_hours_policy, est, state.cash, state.fee_per_order
    )
    summary = engine.summarize(checks)
    decision = engine.decide_placement(
        spec, quote, market.is_open, ctx.slippage_bps, state.after_hours_policy
    )
    return Evaluation(state, quote, market, checks, summary, risk, est, decision, ticker)


def _raise_hard(ev: Evaluation) -> None:
    """Raise the contract error for the first non-overridable failing check."""
    first = ev.summary.hard[0]
    details = {"checks": [c.to_dict() for c in ev.checks]}
    code = {
        "shares": ErrorCode.INSUFFICIENT_SHARES,
        "funds": ErrorCode.INSUFFICIENT_CASH,
        "freshness": ErrorCode.STALE_QUOTE,
        "session_reject": ErrorCode.MARKET_CLOSED,
    }.get(first.key, ErrorCode.VALIDATION_ERROR)
    raise AppError(code, first.detail, details=details)


async def preview_order(
    conn: Connection, user_id: UUID, intent: OrderIntent, ctx: EngineContext
) -> dict[str, Any]:
    ev = await evaluate(conn, user_id, intent, ctx, lock=False)
    est_cost = intent.qty * ev.est_price + (ev.state.fee_per_order if intent.side == "buy" else ZERO)
    return {
        "order": {
            "symbol": intent.symbol, "side": intent.side, "type": intent.type, "qty": intent.qty,
            "limit_price": intent.limit_price, "est_price": ev.est_price, "est_cost": est_cost,
            "est_cash_after": ev.state.cash - est_cost if intent.side == "buy" else ev.state.cash + est_cost,
            "will": None if ev.summary.blocked else ev.decision.action,
        },
        "fill": None,
        "checks": [c.to_dict() for c in ev.checks],
        "blocked": ev.summary.blocked,
        "hard_blocked": bool(ev.summary.hard),
        "market_open": ev.market.is_open,
    }  # fmt: skip


# --------------------------------------------------------------------------------------------------
# fills
# --------------------------------------------------------------------------------------------------


async def execute_fill(
    conn: Connection,
    *,
    portfolio_id: UUID,
    user_id: UUID,
    order_id: UUID,
    symbol: str,
    side: str,
    qty: Decimal,
    price: Decimal,
    slippage_bps: Decimal,
    fee: Decimal,
    now: datetime,
) -> dict[str, Any]:
    """Append the fill + cash_ledger rows and update the derived position. Caller holds the portfolio lock
    and has verified cash/shares. Returns the fill row."""
    fill_id = uuid4()
    await conn.execute(
        "insert into fills (id, order_id, user_id, portfolio_id, symbol, qty, price, slippage_bps, fee, filled_at) "
        "values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        fill_id, order_id, user_id, portfolio_id, symbol, qty, price, slippage_bps, fee, now,
    )  # fmt: skip
    await conn.execute(
        "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount, ref_id) values ($1, $2, $3, 'fill', $4, $5)",
        portfolio_id, user_id, now, cash_effect(side, qty, price), fill_id,  # type: ignore[arg-type]
    )  # fmt: skip
    if fee > 0:
        await conn.execute(
            "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount, ref_id) values ($1, $2, $3, 'fee', $4, $5)",
            portfolio_id, user_id, now, -fee, fill_id,
        )  # fmt: skip
    cur = await conn.fetchrow(
        "select qty, avg_cost from positions where portfolio_id = $1 and symbol = $2 for update",
        portfolio_id,
        symbol,
    )
    lot = Lot(Decimal(cur["qty"]), Decimal(cur["avg_cost"])) if cur else None
    if side == "buy":
        new: Lot | None = apply_buy(lot, qty, price)
    else:
        new, _ = apply_sell(lot, qty, price)
    if new is None:
        await conn.execute(
            "delete from positions where portfolio_id = $1 and symbol = $2", portfolio_id, symbol
        )
    else:
        await conn.execute(
            """
            insert into positions (portfolio_id, symbol, user_id, qty, avg_cost, updated_at) values ($1, $2, $3, $4, $5, $6)
            on conflict (portfolio_id, symbol) do update set qty = excluded.qty, avg_cost = excluded.avg_cost, updated_at = excluded.updated_at
            """,
            portfolio_id, symbol, user_id, new.qty, new.avg_cost, now,
        )  # fmt: skip
    row = await conn.fetchrow("select * from fills where id = $1", fill_id)
    assert row is not None
    return dict(row)


async def _expires_at(market: MarketState, now: datetime) -> datetime:
    return market.close_at or now + timedelta(days=1)


# --------------------------------------------------------------------------------------------------
# place / cancel
# --------------------------------------------------------------------------------------------------


def _same_payload(row: Any, intent: OrderIntent) -> bool:
    return (
        row["symbol"] == intent.symbol
        and row["side"] == intent.side
        and row["type"] == intent.type
        and Decimal(row["qty"]) == intent.qty
        and (row["limit_price"] is None) == (intent.limit_price is None)
        and (intent.limit_price is None or Decimal(row["limit_price"]) == intent.limit_price)
    )


async def place_order(
    conn: Connection, user_id: UUID, intent: OrderIntent, idempotency_key: str, ctx: EngineContext
) -> PlaceResult:
    now = ctx.clock.now()
    pf = await get_portfolio_row(conn, user_id, lock=True)  # serialises everything below
    existing = await conn.fetchrow(
        "select * from orders where user_id = $1 and idempotency_key = $2", user_id, idempotency_key
    )
    if existing is not None:
        if not _same_payload(existing, intent):
            raise conflict("This Idempotency-Key was already used for a different order")
        fills = (await _fills_for(conn, [existing["id"]])).get(existing["id"], [])
        return PlaceResult(
            order_dict(existing, fills),
            fill_dict(fills[0]) if fills else None,
            list(existing["checks"]),
            False,
            replay=True,
        )

    source = intent.source or "manual"
    if intent.proposal_id is not None:
        prop = await conn.fetchrow(
            "select id, symbol, status from proposals where id = $1 and user_id = $2",
            intent.proposal_id,
            user_id,
        )
        if prop is None:
            raise not_found("Proposal not found")
        if prop["status"] not in ("approved", "modified"):
            raise conflict(
                "A proposal-origin order must reference an approved proposal", {"status": prop["status"]}
            )
        if prop["symbol"] != intent.symbol:
            raise validation_error(
                "Order symbol does not match the proposal", {"proposal_symbol": prop["symbol"]}
            )
        source = intent.source or "proposal"

    ev = await evaluate(conn, user_id, intent, ctx, lock=False)  # portfolio is already locked
    checks_json = [c.to_dict() for c in ev.checks]
    if ev.summary.hard:
        _raise_hard(ev)
    overrode = False
    if ev.summary.rule:
        if not intent.override:
            raise AppError(
                ErrorCode.RULE_VIOLATION,
                "This order would break one of your risk rules. Review the checks or confirm the override.",
                details={
                    "violations": [v.to_dict() for v in engine.violations_for(ev.risk, ev.summary.rule)],
                    "checks": checks_json,
                },
            )
        overrode = True

    order_id = uuid4()
    decision = ev.decision
    status = {"fill": "filled", "queue": "queued", "work": "working"}[decision.action]
    expires = now if status == "filled" else await _expires_at(ev.market, now)
    await conn.execute(
        """
        insert into orders (id, portfolio_id, user_id, symbol, side, type, qty, limit_price, tif, status, proposal_id,
                            idempotency_key, override, checks, source, created_at, updated_at, expires_at)
        values ($1, $2, $3, $4, $5, $6, $7, $8, 'day', $9, $10, $11, $12, $13, $14, $15, $15, $16)
        """,
        order_id, pf["id"], user_id, intent.symbol, intent.side, intent.type, intent.qty, intent.limit_price,
        status, intent.proposal_id, idempotency_key, overrode, checks_json, source, now, expires,
    )  # fmt: skip
    fill: dict[str, Any] | None = None
    if decision.action == "fill":
        assert decision.price is not None
        fee = ev.state.fee_per_order
        fill = await execute_fill(
            conn, portfolio_id=pf["id"], user_id=user_id, order_id=order_id, symbol=intent.symbol, side=intent.side,
            qty=intent.qty, price=decision.price, slippage_bps=decision.slippage_bps, fee=fee, now=now,
        )  # fmt: skip
    await write_audit(
        conn, user_id=user_id, action="order.create", entity="order", entity_id=str(order_id),
        details={"symbol": intent.symbol, "side": intent.side, "type": intent.type, "qty": float(intent.qty),
                 "status": status, "override": overrode, "source": source, "proposal_id": str(intent.proposal_id) if intent.proposal_id else None},
    )  # fmt: skip
    ent = {"ticker": intent.symbol, "order_id": str(order_id)}
    props: dict[str, Any] = {
        "side": intent.side,
        "qty": float(intent.qty),
        "type": intent.type,
        "source": source,
    }
    await emit_event(conn, user_id=user_id, ts=now, type="order_placed", entity=ent, props=props)
    if fill is not None:
        await emit_event(
            conn,
            user_id=user_id,
            ts=now,
            type="order_filled",
            entity=ent,
            props={**props, "price": float(fill["price"])},
        )
    if overrode:
        await emit_event(
            conn, user_id=user_id, ts=now, type="rule_override", entity=ent,
            props={"rules": sorted({c.key for c in ev.summary.rule}), "order_id": str(order_id)},
        )  # fmt: skip
    row, fills = await load_order(conn, user_id, order_id)
    if fill is not None:
        await run_after_trade(conn, user_id, ctx)
    return PlaceResult(
        order_dict(row, fills), fill_dict(fills[0]) if fills else None, checks_json, ev.summary.blocked
    )


async def cancel_order(conn: Connection, user_id: UUID, order_id: UUID, ctx: EngineContext) -> dict[str, Any]:
    now = ctx.clock.now()
    await get_portfolio_row(conn, user_id, lock=True)
    row = await conn.fetchrow(
        "select * from orders where id = $1 and user_id = $2 for update", order_id, user_id
    )
    if row is None:
        raise not_found("Order not found")
    if row["status"] not in RESTING:
        raise conflict(f"Order is already {row['status']}", {"status": row["status"]})
    await conn.execute("update orders set status = 'cancelled', updated_at = $2 where id = $1", order_id, now)
    await write_audit(
        conn,
        user_id=user_id,
        action="order.cancel",
        entity="order",
        entity_id=str(order_id),
        details={"symbol": row["symbol"]},
    )
    await emit_event(
        conn,
        user_id=user_id,
        ts=now,
        type="order_cancelled",
        entity={"ticker": row["symbol"], "order_id": str(order_id)},
    )
    out, fills = await load_order(conn, user_id, order_id)
    await run_after_trade(conn, user_id, ctx)
    return order_dict(out, fills)


# --------------------------------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------------------------------


def _encode_cursor(ts: datetime, oid: UUID) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{oid}".encode()).decode()


def _decode_cursor(cur: str) -> tuple[datetime, UUID]:
    try:
        ts, oid = base64.urlsafe_b64decode(cur.encode()).decode().split("|")
        return datetime.fromisoformat(ts), UUID(oid)
    except Exception as exc:
        raise validation_error("Invalid cursor") from exc


async def list_orders(
    conn: Connection, user_id: UUID, *, status: str | None, limit: int, cursor: str | None
) -> dict[str, Any]:
    args: list[Any] = [user_id]
    where = ["user_id = $1"]
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if cursor:
        ts, oid = _decode_cursor(cursor)
        args += [ts, oid]
        where.append(f"(created_at, id) < (${len(args) - 1}, ${len(args)})")
    args.append(limit + 1)
    rows = await conn.fetch(
        f"select * from orders where {' and '.join(where)} order by created_at desc, id desc limit ${len(args)}",  # noqa: S608 - clauses are static, values are bound
        *args,
    )
    page = rows[:limit]
    fills = await _fills_for(conn, [r["id"] for r in page])
    nxt = _encode_cursor(page[-1]["created_at"], page[-1]["id"]) if len(rows) > limit else None
    return {"items": [order_dict(r, fills.get(r["id"], [])) for r in page], "next_cursor": nxt}


# --------------------------------------------------------------------------------------------------
# sweeper: queued -> fill at the open, working limits cross, day orders expire
# --------------------------------------------------------------------------------------------------


async def process_orders(
    conn: Connection, ctx: EngineContext, *, user_id: UUID | None = None
) -> dict[str, int]:
    """One pass over resting orders (all users with an owner connection, or one user). Returns counters."""
    now = ctx.clock.now()
    market = ctx.market_now()
    stats = {"filled": 0, "expired": 0, "rejected": 0, "activated": 0}
    if user_id is None:
        pairs = await conn.fetch(
            "select distinct user_id, portfolio_id from orders where status = any($1::text[])", list(RESTING)
        )
    else:
        pairs = await conn.fetch(
            "select distinct user_id, portfolio_id from orders where user_id = $1 and status = any($2::text[])",
            user_id,
            list(RESTING),
        )
    for pair in pairs:
        uid, pid = pair["user_id"], pair["portfolio_id"]
        touched = False
        async with conn.transaction():
            await conn.fetchrow("select id from portfolios where id = $1 for update", pid)
            orders = await conn.fetch(
                "select * from orders where portfolio_id = $1 and status = any($2::text[]) order by created_at, id for update",
                pid, list(RESTING),
            )  # fmt: skip
            quotes = await quotes_for(ctx.prices, sorted({o["symbol"] for o in orders}), conn)
            pf = await conn.fetchrow("select fee_per_order from portfolios where id = $1", pid)
            fee = Decimal(pf["fee_per_order"]) if pf else ZERO
            for o in orders:
                spec = engine.OrderSpec(
                    o["symbol"],
                    o["side"],
                    o["type"],
                    Decimal(o["qty"]),
                    None if o["limit_price"] is None else Decimal(o["limit_price"]),
                )
                expired_by_time = (
                    o["status"] == "working" and o["expires_at"] is not None and now >= o["expires_at"]
                )
                d = engine.decide_resting(
                    spec,
                    o["status"],
                    quotes.get(o["symbol"]),
                    market.is_open and not expired_by_time,
                    ctx.slippage_bps,
                )
                ent = {"ticker": o["symbol"], "order_id": str(o["id"])}
                if d.action == "reject" and d.reason == "expired":
                    await conn.execute(
                        "update orders set status = 'expired', reject_reason = 'day order expired at the close', updated_at = $2 where id = $1",
                        o["id"],
                        now,
                    )
                    await emit_event(conn, user_id=uid, ts=now, type="order_expired", entity=ent)
                    stats["expired"] += 1
                    touched = True
                elif d.action == "work" and o["status"] == "queued":
                    await conn.execute(
                        "update orders set status = 'working', expires_at = $2, updated_at = $3 where id = $1",
                        o["id"],
                        await _expires_at(market, now),
                        now,
                    )
                    stats["activated"] += 1
                elif d.action in {"queue", "work"}:
                    continue
                elif d.action == "fill":
                    assert d.price is not None
                    # re-check affordability at fill time (cash may have been spent since the order was queued)
                    cash = await cash_balance(conn, pid)
                    held = await conn.fetchval(
                        "select qty from positions where portfolio_id = $1 and symbol = $2", pid, o["symbol"]
                    )
                    held_q = Decimal(held) if held is not None else ZERO
                    problem = None
                    if spec.side == "buy" and spec.qty * d.price + fee > cash:
                        problem = "insufficient_cash"
                    elif spec.side == "sell" and spec.qty > held_q:
                        problem = "insufficient_shares"
                    if problem:
                        await conn.execute(
                            "update orders set status = 'rejected', reject_reason = $2, updated_at = $3 where id = $1",
                            o["id"],
                            problem,
                            now,
                        )
                        await emit_event(
                            conn,
                            user_id=uid,
                            ts=now,
                            type="order_rejected",
                            entity=ent,
                            props={"reason": problem},
                        )
                        stats["rejected"] += 1
                        touched = True
                        continue
                    await execute_fill(
                        conn, portfolio_id=pid, user_id=uid, order_id=o["id"], symbol=o["symbol"], side=o["side"],
                        qty=spec.qty, price=d.price, slippage_bps=d.slippage_bps, fee=fee, now=now,
                    )  # fmt: skip
                    await conn.execute(
                        "update orders set status = 'filled', updated_at = $2 where id = $1", o["id"], now
                    )
                    await write_audit(
                        conn,
                        user_id=uid,
                        action="order.fill",
                        entity="order",
                        entity_id=str(o["id"]),
                        details={"symbol": o["symbol"], "price": float(d.price), "via": "sweeper"},
                    )
                    await emit_event(conn, user_id=uid, ts=now, type="order_filled", entity=ent,
                                     props={"side": o["side"], "qty": float(spec.qty), "price": float(d.price), "type": o["type"], "source": o["source"]})  # fmt: skip
                    stats["filled"] += 1
                    touched = True
            if touched:
                await run_after_trade(conn, uid, ctx)
    return stats


# --------------------------------------------------------------------------------------------------
# equity snapshots
# --------------------------------------------------------------------------------------------------


async def take_equity_snapshot(
    conn: Connection, user_id: UUID, ctx: EngineContext, ts: datetime | None = None
) -> dict[str, Any]:
    """Record `(equity, cash, spy_equity_norm)` for the user's portfolio at `ts` (default: clock now).

    `spy_equity_norm` = SPY normalised to the portfolio's FIRST snapshot equity (so both curves start equal)."""
    ts = ts or ctx.clock.now()
    state = await load_state(conn, user_id, ctx)
    spy_q = (await quotes_for(ctx.prices, ["SPY"], conn)).get("SPY")
    first = await conn.fetchrow(
        "select equity, spy_equity_norm, ts from equity_snapshots where portfolio_id = $1 order by ts limit 1",
        state.portfolio_id,
    )
    spy_norm: Decimal | None = None
    if spy_q is not None:
        if first is None or first["spy_equity_norm"] is None:
            spy_norm = state.equity
        else:
            # SPY level at the first snapshot = stored spy_equity_norm's anchor: use the stored close nearest that day
            base = await conn.fetchval(
                "select close from prices_daily where symbol = 'SPY' and date <= $1 order by date desc limit 1",
                first["ts"].date(),
            )
            spy_norm = (Decimal(first["equity"]) * spy_q.price / Decimal(base)) if base else None
    equity = state.equity.quantize(Decimal("0.01"))
    await conn.execute(
        """
        insert into equity_snapshots (portfolio_id, user_id, ts, equity, cash, spy_equity_norm) values ($1, $2, $3, $4, $5, $6)
        on conflict (portfolio_id, ts) do update set equity = excluded.equity, cash = excluded.cash, spy_equity_norm = excluded.spy_equity_norm
        """,
        state.portfolio_id, user_id, ts, equity, state.cash.quantize(Decimal("0.01")),
        None if spy_norm is None else spy_norm.quantize(Decimal("0.000001")),
    )  # fmt: skip
    return {"ts": ts, "equity": equity, "cash": state.cash, "spy_equity_norm": spy_norm}


async def snapshot_all(
    conn: Connection, ctx: EngineContext, *, min_gap: timedelta = timedelta(minutes=15)
) -> int:
    """Owner-connection job: snapshot every portfolio whose last snapshot is older than `min_gap`."""
    now = ctx.clock.now()
    rows = await conn.fetch(
        "select p.user_id, (select max(ts) from equity_snapshots e where e.portfolio_id = p.id) as last from portfolios p"
    )
    n = 0
    for r in rows:
        if r["last"] is None or now - r["last"] >= min_gap:
            async with conn.transaction():
                await take_equity_snapshot(conn, r["user_id"], ctx, now)
            n += 1
    return n


# --------------------------------------------------------------------------------------------------
# corporate actions (T-PAPER-02) and reconciliation (T-PAPER-04)
# --------------------------------------------------------------------------------------------------


async def _split_records(conn: Connection, symbols: list[str]) -> list[SplitRecord]:
    rows = await conn.fetch(
        "select symbol, ex_date, ratio from corporate_actions where type = 'split' and symbol = any($1::text[]) and ratio is not null",
        symbols,
    )
    return [SplitRecord(r["symbol"], r["ex_date"], Decimal(r["ratio"])) for r in rows]


async def rebuild_from_fills(conn: Connection, portfolio_id: UUID) -> dict[str, Lot]:
    rows = await conn.fetch(
        "select f.symbol, o.side, f.qty, f.price, f.filled_at, f.fee from fills f join orders o on o.id = f.order_id "
        "where f.portfolio_id = $1 order by f.filled_at, f.seq",
        portfolio_id,
    )
    fills = [
        FillRecord(
            r["symbol"], r["side"], Decimal(r["qty"]), Decimal(r["price"]), r["filled_at"], Decimal(r["fee"])
        )
        for r in rows
    ]
    return rebuild_positions(fills, await _split_records(conn, sorted({f.symbol for f in fills})))


async def apply_split(conn: Connection, symbol: str, ex_date: date, ratio: Decimal, now: datetime) -> int:
    """Owner-connection job: record the split in `corporate_actions` and re-derive every holder's position
    (qty x ratio, avg cost / ratio, so market value and P&L are unchanged). Idempotent. Returns holders adjusted."""
    if ratio <= 0:
        raise ValueError("split ratio must be positive")
    await conn.execute(
        "insert into corporate_actions (symbol, type, ex_date, ratio) values ($1, 'split', $2, $3) on conflict (symbol, type, ex_date) do nothing",
        symbol, ex_date, ratio,
    )  # fmt: skip
    holders = await conn.fetch(
        "select portfolio_id, user_id, qty, avg_cost from positions where symbol = $1 and qty > 0 for update",
        symbol,
    )
    adjusted = 0
    for h in holders:
        done = await conn.fetchval(
            "select 1 from position_adjustments where portfolio_id = $1 and symbol = $2 and kind = 'split' and ex_date = $3",
            h["portfolio_id"], symbol, ex_date,
        )  # fmt: skip
        if done or now < split_effective_at(ex_date).astimezone(UTC):
            continue
        rebuilt = (await rebuild_from_fills(conn, h["portfolio_id"])).get(symbol)
        if rebuilt is None:
            continue
        await conn.execute(
            "update positions set qty = $3, avg_cost = $4, updated_at = $5 where portfolio_id = $1 and symbol = $2",
            h["portfolio_id"], symbol, rebuilt.qty, rebuilt.avg_cost, now,
        )  # fmt: skip
        await conn.execute(
            "insert into position_adjustments (portfolio_id, user_id, symbol, kind, ex_date, ratio, qty_before, qty_after, "
            "avg_cost_before, avg_cost_after, applied_at) values ($1, $2, $3, 'split', $4, $5, $6, $7, $8, $9, $10)",
            h["portfolio_id"], h["user_id"], symbol, ex_date, ratio, h["qty"], rebuilt.qty, h["avg_cost"], rebuilt.avg_cost, now,
        )  # fmt: skip
        adjusted += 1
    return adjusted


async def reconcile(
    conn: Connection, portfolio_id: UUID, prices: dict[str, Decimal] | None = None
) -> dict[str, Any]:
    """Accounting audit of one portfolio: cash == ledger == deposits + fills, positions == rebuilt-from-fills,
    and `cash + sum(qty x last) = equity`. `prices` default to the stored daily closes."""
    ledger_cash = await cash_balance(conn, portfolio_id)
    non_fill = Decimal(
        await conn.fetchval(
            "select coalesce(sum(amount), 0) from cash_ledger where portfolio_id = $1 and kind not in ('fill', 'fee')",
            portfolio_id,
        )
    )
    fill_rows = await conn.fetch(
        "select f.symbol, o.side, f.qty, f.price, f.fee from fills f join orders o on o.id = f.order_id where f.portfolio_id = $1",
        portfolio_id,
    )
    from_fills = sum(
        (
            cash_effect(r["side"], Decimal(r["qty"]), Decimal(r["price"]), Decimal(r["fee"]))
            for r in fill_rows
        ),
        ZERO,
    )
    rebuilt = await rebuild_from_fills(conn, portfolio_id)
    held = {
        r["symbol"]: (Decimal(r["qty"]), Decimal(r["avg_cost"]))
        for r in await conn.fetch(
            "select symbol, qty, avg_cost from positions where portfolio_id = $1 and qty > 0", portfolio_id
        )
    }
    expected = {s: (lot.qty, lot.avg_cost) for s, lot in rebuilt.items()}
    mismatches = sorted(
        set(held) ^ set(expected) | {s for s in held if s in expected and held[s] != expected[s]}
    )
    last = (
        prices
        if prices is not None
        else {s: c.price for s, c in (await latest_closes(conn, list(held))).items()}
    )
    value = sum((q * last.get(s, avg) for s, (q, avg) in held.items()), ZERO)
    return {
        "ok": ledger_cash == non_fill + from_fills and not mismatches,
        "cash": ledger_cash,
        "cash_from_fills": non_fill + from_fills,
        "positions_value": value,
        "equity": ledger_cash + value,
        "position_mismatches": mismatches,
    }
