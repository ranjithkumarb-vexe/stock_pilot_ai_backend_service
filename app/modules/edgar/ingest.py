"""XBRL ingest orchestration: `companyfacts` -> `xbrl_facts` for a set of tickers (resumable, idempotent)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.core.db import Connection
from app.core.logging import get_logger
from app.modules.edgar.client import EdgarClient, EdgarError, companyfacts_url
from app.modules.edgar.universe import BY_SYMBOL
from app.modules.edgar.xbrl import parse_companyfacts, store_facts

log = get_logger("app.edgar.ingest")

COMPANYFACTS_MAX_AGE_S = 24 * 3600


@dataclass(frozen=True, slots=True)
class TickerRef:
    symbol: str
    cik: str


@dataclass(slots=True)
class XbrlIngestResult:
    symbol: str
    cik: str
    parsed: int = 0
    inserted: int = 0
    from_cache: bool = False
    error: str | None = None


async def ensure_universe_rows(conn: Connection, symbols: Iterable[str]) -> None:
    """Insert missing ticker rows from the built-in universe (never overwrites the seed's data)."""
    for sym in symbols:
        entry = BY_SYMBOL.get(sym)
        if entry is None:
            continue
        await conn.execute(
            "insert into tickers (symbol, cik, name, sector, exchange, in_universe) "
            "values ($1, $2, $3, $4, $5, true) on conflict (symbol) do nothing",
            entry.symbol,
            entry.cik,
            entry.name,
            entry.sector,
            entry.exchange,
        )
        # a seed row without a CIK is completed, never overwritten
        await conn.execute("update tickers set cik = $2 where symbol = $1 and cik is null", sym, entry.cik)


async def universe_refs(conn: Connection, symbols: Sequence[str] | None = None) -> list[TickerRef]:
    rows = await conn.fetch(
        "select symbol, cik from tickers where cik is not null and in_universe "
        "and ($1::text[] is null or symbol = any($1::text[])) order by symbol",
        list(symbols) if symbols else None,
    )
    return [TickerRef(r["symbol"], r["cik"]) for r in rows]


async def ingest_xbrl_for(
    conn: Connection, client: EdgarClient, ref: TickerRef, *, force: bool = False
) -> XbrlIngestResult:
    res = XbrlIngestResult(symbol=ref.symbol, cik=ref.cik)
    try:
        fetched = await client.get(companyfacts_url(ref.cik), max_age_s=COMPANYFACTS_MAX_AGE_S, force=force)
    except EdgarError as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        log.error("xbrl fetch failed", extra={"symbol": ref.symbol, "error": res.error})
        return res
    res.from_cache = fetched.from_cache
    facts = parse_companyfacts(fetched.json())
    res.parsed = len(facts)
    res.inserted = await store_facts(conn, ref.symbol, facts)
    log.info(
        "xbrl ingested",
        extra={
            "symbol": ref.symbol,
            "parsed": res.parsed,
            "inserted": res.inserted,
            "cached": res.from_cache,
        },
    )
    return res
