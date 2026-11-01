"""Command line: `python -m app.modules.edgar.cli <command>` (see `make ingest-xbrl`, `make ingest-filings`).

Commands
  ingest-xbrl     [--tickers A,B] [--force]            companyfacts -> xbrl_facts
  record-fixtures --tickers AAPL,JNJ --out DIR          download + trim real companyfacts for tests
Filing ingest lives in `app.modules.research.cli` (it needs the chunking/embedding pipeline).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.db import Database, set_database, system_tx
from app.modules.edgar.client import EdgarClient, companyfacts_url
from app.modules.edgar.ingest import ensure_universe_rows, ingest_xbrl_for, universe_refs
from app.modules.edgar.universe import BY_SYMBOL, UNIVERSE
from app.modules.edgar.xbrl import CONCEPT_ALLOWLIST


@asynccontextmanager
async def open_database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings)
    await db.open()
    set_database(db)
    try:
        yield db
    finally:
        set_database(None)
        await db.close()


def _split(value: str | None) -> list[str] | None:
    return [s.strip().upper() for s in value.split(",") if s.strip()] if value else None


async def cmd_ingest_xbrl(settings: Settings, tickers: list[str] | None, force: bool) -> dict[str, Any]:
    async with open_database(settings), EdgarClient(settings.sec_user_agent) as client:
        async with system_tx() as conn:
            await ensure_universe_rows(conn, tickers or [u.symbol for u in UNIVERSE])
        async with system_tx() as conn:
            refs = await universe_refs(conn, tickers)
        results = []
        for ref in refs:
            async with system_tx() as conn:
                results.append(await ingest_xbrl_for(conn, client, ref, force=force))
        async with system_tx() as conn:
            total = await conn.fetchval("select count(*) from xbrl_facts")
            tickers_with = await conn.fetchval("select count(distinct symbol) from xbrl_facts")
        return {
            "tickers_requested": len(refs),
            "tickers_ok": sum(1 for r in results if r.error is None),
            "tickers_with_facts": tickers_with,
            "facts_total": total,
            "requests": client.request_count,
            "results": [
                {
                    "symbol": r.symbol,
                    "parsed": r.parsed,
                    "inserted": r.inserted,
                    "cached": r.from_cache,
                    "error": r.error,
                }
                for r in results
            ],
        }


def trim_companyfacts(payload: dict[str, Any], since_year: int = 2021) -> dict[str, Any]:
    """Keep allow-listed concepts, 10-K-filed entries ending in/after `since_year` (fixture size control)."""
    out_facts: dict[str, Any] = {}
    for taxonomy, allowed in CONCEPT_ALLOWLIST.items():
        src = payload.get("facts", {}).get(taxonomy, {})
        kept: dict[str, Any] = {}
        for name in sorted(allowed & src.keys()):
            units: dict[str, list[Any]] = {}
            for unit, entries in src[name].get("units", {}).items():
                rows = [
                    e
                    for e in entries
                    if (e.get("form") in ("10-K", "10-K/A") or taxonomy == "dei")
                    and str(e.get("end", ""))[:4].isdigit()
                    and int(str(e["end"])[:4]) >= since_year
                ]
                if rows:
                    units[unit] = rows
            if units:
                kept[name] = {"label": src[name].get("label"), "units": units}
        if kept:
            out_facts[taxonomy] = kept
    return {"cik": payload.get("cik"), "entityName": payload.get("entityName"), "facts": out_facts}


async def cmd_record_fixtures(settings: Settings, tickers: list[str], out: Path) -> dict[str, Any]:
    written: dict[str, int] = {}
    async with EdgarClient(settings.sec_user_agent) as client:
        for sym in tickers:
            entry = BY_SYMBOL[sym]
            payload = await client.get_json(companyfacts_url(entry.cik), max_age_s=24 * 3600)
            trimmed = trim_companyfacts(payload)
            path = out / f"companyfacts_{sym}.json"
            trimmed_json = json.dumps(trimmed, separators=(",", ":"), sort_keys=True)
            path.write_text(trimmed_json)
            written[sym] = len(trimmed_json)
    return {"written_bytes": written}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.modules.edgar.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("ingest-xbrl")
    p1.add_argument("--tickers")
    p1.add_argument("--force", action="store_true")
    p2 = sub.add_parser("record-fixtures")
    p2.add_argument("--tickers", required=True)
    p2.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.cmd == "ingest-xbrl":
        result = asyncio.run(cmd_ingest_xbrl(settings, _split(args.tickers), args.force))
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(cmd_record_fixtures(settings, _split(args.tickers) or [], args.out))
    print(json.dumps(result, indent=2, default=str))
    failed = isinstance(result, dict) and result.get("tickers_ok", 1) != result.get("tickers_requested", 1)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
