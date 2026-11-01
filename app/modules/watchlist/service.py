from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.audit import write_audit
from app.core.db import Connection
from app.core.errors import AppError, ErrorCode, validation_error
from app.modules.paper.context import EngineContext
from app.modules.paper.pricing import quotes_for
from app.modules.tradingcommon.events import universe_row
from app.modules.tradingcommon.quotes import quote_dict

MAX_WATCHLIST = 30  # SPEC 6.4: free-tier subscription cap


def unsupported(symbol: str) -> AppError:
    return AppError(
        ErrorCode.UNSUPPORTED_SYMBOL,
        f"{symbol} is not in the supported universe",
        details={"symbol": symbol},
    )


async def list_universe(conn: Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "select symbol, name, sector, industry, exchange from tickers where in_universe order by symbol"
    )
    return [dict(r) for r in rows]


async def list_watchlist(conn: Connection, user_id: UUID, ctx: EngineContext) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        select w.symbol, w.added_at, t.name, t.sector, t.industry, t.exchange
          from watchlist_items w join tickers t on t.symbol = w.symbol
         where w.user_id = $1 order by w.added_at, w.symbol
        """,
        user_id,
    )
    quotes = await quotes_for(ctx.prices, [r["symbol"] for r in rows], conn)
    out: list[dict[str, Any]] = []
    for r in rows:
        q = quotes.get(r["symbol"])
        out.append(
            {
                "symbol": r["symbol"],
                "name": r["name"],
                "sector": r["sector"],
                "industry": r["industry"],
                "exchange": r["exchange"],
                "added_at": r["added_at"].isoformat().replace("+00:00", "Z"),
                "quote": None if q is None else quote_dict(q),
            }
        )
    return out


async def add_symbol(conn: Connection, user_id: UUID, symbol: str, ctx: EngineContext) -> dict[str, Any]:
    if await universe_row(conn, symbol) is None:
        raise unsupported(symbol)
    exists = await conn.fetchval(
        "select 1 from watchlist_items where user_id = $1 and symbol = $2", user_id, symbol
    )
    if not exists:
        count = await conn.fetchval("select count(*) from watchlist_items where user_id = $1", user_id)
        if int(count) >= MAX_WATCHLIST:
            raise validation_error(f"Watchlist is limited to {MAX_WATCHLIST} symbols", {"max": MAX_WATCHLIST})
        await conn.execute(
            "insert into watchlist_items (user_id, symbol, added_at) values ($1, $2, $3)",
            user_id,
            symbol,
            ctx.clock.now(),
        )
        await write_audit(conn, user_id=user_id, action="watchlist.add", entity="watchlist", entity_id=symbol)
    items = await list_watchlist(conn, user_id, ctx)
    return next(i for i in items if i["symbol"] == symbol)


async def remove_symbol(conn: Connection, user_id: UUID, symbol: str) -> None:
    status = await conn.execute(
        "delete from watchlist_items where user_id = $1 and symbol = $2", user_id, symbol
    )
    if status.endswith(" 1"):
        await write_audit(
            conn, user_id=user_id, action="watchlist.remove", entity="watchlist", entity_id=symbol
        )
