"""Loading a user's paper portfolio (cash, positions, rules, valuation) from the database.

Cash is ALWAYS the sum of `cash_ledger`; positions come from the derived `positions` table (verified against
`rebuild_positions` by `reconcile`). Valuation uses the engine context's price source with the stored daily
close as fallback, and the position's average cost as a last resort so a missing quote never crashes a page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.errors import not_found
from app.core.pricesource import Quote
from app.modules.paper.context import EngineContext
from app.modules.paper.pricing import quotes_for
from app.modules.risk import Portfolio, Position, Rules, default_rules_for_tier

ZERO = Decimal(0)
TRADE_STATUSES = ("filled", "queued", "working")


@dataclass(frozen=True, slots=True)
class PositionState:
    symbol: str
    name: str
    sector: str | None
    qty: Decimal
    avg_cost: Decimal
    price: Decimal
    prev_close: Decimal | None
    quote: Quote | None

    @property
    def value(self) -> Decimal:
        return self.qty * self.price

    @property
    def change_pct(self) -> Decimal:
        if self.prev_close is None or self.prev_close == 0:
            return ZERO
        return (self.price / self.prev_close - 1) * 100


@dataclass(frozen=True, slots=True)
class PortfolioState:
    portfolio_id: UUID
    user_id: UUID
    name: str
    starting_cash: Decimal
    cash: Decimal
    positions: tuple[PositionState, ...]
    trades_7d: int
    rules: Rules
    after_hours_policy: str
    fee_per_order: Decimal
    created_at: datetime

    def to_risk(self) -> Portfolio:
        return Portfolio(
            self.cash,
            tuple(
                Position(p.symbol, p.sector, p.qty, p.price, p.prev_close, p.avg_cost, p.name)
                for p in self.positions
            ),
            self.trades_7d,
        )

    @property
    def invested(self) -> Decimal:
        return sum((p.value for p in self.positions), ZERO)

    @property
    def equity(self) -> Decimal:
        return self.cash + self.invested

    def position(self, symbol: str) -> PositionState | None:
        return next((p for p in self.positions if p.symbol == symbol), None)


async def get_portfolio_row(conn: Connection, user_id: UUID, *, lock: bool = False) -> dict[str, Any]:
    sql = (
        "select * from portfolios where user_id = $1 order by created_at limit 1 for update"
        if lock
        else "select * from portfolios where user_id = $1 order by created_at limit 1"
    )
    row = await conn.fetchrow(sql, user_id)
    if row is None:
        raise not_found("No paper portfolio yet - complete onboarding first")
    return dict(row)


async def cash_balance(conn: Connection, portfolio_id: UUID) -> Decimal:
    return Decimal(
        await conn.fetchval(
            "select coalesce(sum(amount), 0) from cash_ledger where portfolio_id = $1", portfolio_id
        )
    )


async def load_rules(conn: Connection, user_id: UUID) -> Rules:
    rows = await conn.fetch("select rule_key, value from risk_rules where user_id = $1", user_id)
    have = {r["rule_key"]: r["value"] for r in rows}
    tier = await conn.fetchval("select risk_tier_stated from profiles where user_id = $1", user_id)
    base = default_rules_for_tier(int(tier or 3)).to_dict()
    merged = {k: have.get(k, base[k]) for k in ("pos", "sec", "dd", "trades", "cash")}
    return Rules.from_mapping(merged)


async def trades_in_window(conn: Connection, user_id: UUID, now: datetime, days: int = 7) -> int:
    return int(
        await conn.fetchval(
            "select count(*) from orders where user_id = $1 and status = any($2::text[]) and created_at > $3",
            user_id,
            list(TRADE_STATUSES),
            now - timedelta(days=days),
        )
    )


async def load_state(
    conn: Connection, user_id: UUID, ctx: EngineContext, *, lock: bool = False
) -> PortfolioState:
    pf = await get_portfolio_row(conn, user_id, lock=lock)
    pid: UUID = pf["id"]
    cash = await cash_balance(conn, pid)
    rows = await conn.fetch(
        """
        select p.symbol, p.qty, p.avg_cost, t.name, t.sector
          from positions p join tickers t on t.symbol = p.symbol
         where p.portfolio_id = $1 and p.qty > 0 order by p.symbol
        """,
        pid,
    )
    quotes = await quotes_for(ctx.prices, [r["symbol"] for r in rows], conn)
    positions: list[PositionState] = []
    for r in rows:
        q = quotes.get(r["symbol"])
        price = q.price if q is not None else Decimal(r["avg_cost"])
        positions.append(
            PositionState(
                r["symbol"], r["name"], r["sector"], Decimal(r["qty"]), Decimal(r["avg_cost"]), price,
                q.prev_close if q is not None else None, q,
            )
        )  # fmt: skip
    return PortfolioState(
        portfolio_id=pid,
        user_id=user_id,
        name=str(pf["name"]),
        starting_cash=Decimal(pf["starting_cash"]),
        cash=cash,
        positions=tuple(positions),
        trades_7d=await trades_in_window(conn, user_id, ctx.clock.now()),
        rules=await load_rules(conn, user_id),
        after_hours_policy=str(pf["after_hours_policy"]),
        fee_per_order=Decimal(pf["fee_per_order"]),
        created_at=pf["created_at"],
    )
