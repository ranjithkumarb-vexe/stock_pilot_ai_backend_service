"""Price resolution for the paper engine.

Runtime source = `request.app.state.price_source` (installed by stream C's market-data module). When it is
absent - or cannot quote a symbol - the engine falls back to the last stored daily close in `prices_daily`
(`DbPriceSource`), so trading never blocks on stream C.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from app.core.db import Connection, Pool
from app.core.pricesource import PriceSource, Quote, QuoteUnavailableError
from app.modules.paper.market import effective_market_state

_LATEST_SQL = """
select symbol, close, prev, date from (
    select symbol, close, date,
           lag(close) over (partition by symbol order by date) as prev,
           row_number() over (partition by symbol order by date desc) as rn
      from prices_daily
     where symbol = any($1::text[])
) t where rn = 1
"""


@dataclass(frozen=True, slots=True)
class DbClose:
    price: Decimal
    prev_close: Decimal | None
    day: date


async def latest_closes(conn: Connection, symbols: Sequence[str]) -> dict[str, DbClose]:
    if not symbols:
        return {}
    rows = await conn.fetch(_LATEST_SQL, list(symbols))
    return {
        r["symbol"]: DbClose(
            Decimal(r["close"]), None if r["prev"] is None else Decimal(r["prev"]), r["date"]
        )
        for r in rows
    }


def _close_ts(day: date) -> datetime:
    return datetime.combine(day, time(21, 0), tzinfo=UTC)


class DbPriceSource:
    """`PriceSource` over `prices_daily` (last close). Freshness is `closed` outside the session and `delayed`
    inside it (a stored close is never `stale`, so it never blocks market orders)."""

    name = "db_last_close"

    def __init__(self, pool: Pool, app_state: Any = None, now: Any = None) -> None:
        self._pool = pool
        self._state = app_state
        self._now = now  # callable returning aware datetime, defaults to the wall clock

    def _market_open(self) -> bool:
        now = self._now() if self._now else datetime.now(UTC)
        return effective_market_state(self._state, now).is_open

    async def get_last(self, symbol: str) -> Quote:
        quotes = await self.get_many([symbol])
        try:
            return quotes[symbol]
        except KeyError as exc:
            raise QuoteUnavailableError(symbol) from exc

    async def get_many(self, symbols: Sequence[str]) -> dict[str, Quote]:
        async with self._pool.acquire() as conn:
            closes = await latest_closes(conn, list(symbols))
        is_open = self._market_open()
        out: dict[str, Quote] = {}
        for sym, c in closes.items():
            out[sym] = Quote(
                symbol=sym,
                price=c.price,
                ts=_close_ts(c.day),
                source="simulated",
                prev_close=c.prev_close,
                session="regular" if is_open else "closed",
                freshness="delayed" if is_open else "closed",
            )
        return out

    async def subscribe(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        for sym in symbols:
            yield await self.get_last(sym)


async def quotes_for(
    source: PriceSource, symbols: Sequence[str], conn: Connection | None = None
) -> dict[str, Quote]:
    """Best-effort quotes. Live source first; a symbol it cannot quote falls back to the DB close (when `conn`
    is given). Never raises for an individual symbol."""
    uniq = list(dict.fromkeys(symbols))

    async def one(sym: str) -> tuple[str, Quote | None]:
        try:
            return sym, await source.get_last(sym)
        except (QuoteUnavailableError, LookupError):
            return sym, None
        except Exception:
            return sym, None

    results = await asyncio.gather(*(one(s) for s in uniq))
    out = {s: q for s, q in results if q is not None}
    missing = [s for s in uniq if s not in out]
    if missing and conn is not None:
        closes = await latest_closes(conn, missing)
        for sym, c in closes.items():
            out[sym] = Quote(
                symbol=sym,
                price=c.price,
                ts=_close_ts(c.day),
                source="simulated",
                prev_close=c.prev_close,
                session="closed",
                freshness="closed",
            )
    # live quote without prev_close: borrow the stored one so day change still works
    need_prev = [s for s, q in out.items() if q.prev_close is None]
    if need_prev and conn is not None:
        closes = await latest_closes(conn, need_prev)
        for sym in need_prev:
            stored = closes.get(sym)
            if stored is not None:
                c = stored
                q = out[sym]
                out[sym] = Quote(
                    symbol=q.symbol, price=q.price, ts=q.ts, source=q.source,
                    prev_close=c.price if q.ts.date() > c.day else c.prev_close,
                    bid=q.bid, ask=q.ask, session=q.session, freshness=q.freshness,
                )  # fmt: skip
    return out


class ConnPriceSource(DbPriceSource):
    """Last-close source bound to ONE open connection (seed / jobs that must see their own uncommitted rows)."""

    name = "conn_last_close"

    def __init__(self, conn: Connection, market_open: bool = False) -> None:
        self._conn = conn
        self._open = market_open

    async def get_many(self, symbols: Sequence[str]) -> dict[str, Quote]:
        closes = await latest_closes(self._conn, list(symbols))
        return {
            s: Quote(
                s, c.price, _close_ts(c.day), "simulated", prev_close=c.prev_close,
                session="regular" if self._open else "closed", freshness="delayed" if self._open else "closed",
            )
            for s, c in closes.items()
        }  # fmt: skip
