"""`python -m app.modules.research.cli ingest-filings [--tickers A,B] [--n10k 1] [--n8k 3] [--force]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from app.core.config import Settings, get_settings
from app.core.db import system_tx, vec_literal
from app.modules.edgar.cli import open_database
from app.modules.edgar.client import EdgarClient
from app.modules.edgar.ingest import ensure_universe_rows, universe_refs
from app.modules.edgar.universe import UNIVERSE
from app.modules.research.embedding import HashingEmbedder
from app.modules.research.ingest import ingest_ticker


def _split(value: str | None) -> list[str] | None:
    return [s.strip().upper() for s in value.split(",") if s.strip()] if value else None


async def cmd_ingest_filings(
    settings: Settings, tickers: list[str] | None, n_10k: int, n_8k: int, force: bool
) -> dict[str, Any]:
    async with open_database(settings), EdgarClient(settings.sec_user_agent) as client:
        async with system_tx() as conn:
            await ensure_universe_rows(conn, tickers or [u.symbol for u in UNIVERSE])
            refs = await universe_refs(conn, tickers)
        results = []
        for ref in refs:
            async with system_tx() as conn:  # one transaction per ticker: resumable at ticker granularity
                results.append(await ingest_ticker(conn, client, ref, n_10k=n_10k, n_8k=n_8k, force=force))
        async with system_tx() as conn:
            totals = await conn.fetchrow(
                "select count(*) as chunks, count(*) filter (where quarantined) as quarantined, "
                "count(distinct symbol) as tickers, count(distinct document_id) as documents from doc_chunks"
            )
        return {
            "requests": client.request_count,
            "db_totals": dict(totals) if totals else {},
            "tickers": [
                {
                    "symbol": r.symbol,
                    "error": r.error,
                    "documents": [
                        {
                            "form": d.form,
                            "accession_no": d.accession_no,
                            "status": d.status,
                            "chunks": d.chunks,
                            "quarantined": d.quarantined,
                            "tables": d.tables,
                            "sections": d.sections,
                            "thin_sections": d.thin_sections,
                            "error": d.error,
                        }
                        for d in r.documents
                    ],
                }
                for r in results
            ],
        }


async def cmd_reembed(settings: Settings, batch: int = 500) -> dict[str, Any]:
    emb = HashingEmbedder()
    n = 0
    async with open_database(settings):
        while True:
            async with system_tx() as conn:
                rows = await conn.fetch(
                    "select id, text from doc_chunks where not quarantined and embedding is null limit $1",
                    batch,
                )
                if not rows:
                    break
                for r in rows:
                    await conn.execute(
                        "update doc_chunks set embedding = $2::vector where id = $1",
                        r["id"],
                        vec_literal(emb.embed(r["text"])),
                    )
                n += len(rows)
    return {"embedded": n, "version": emb.version}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.modules.research.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("ingest-filings")
    p1.add_argument("--tickers")
    p1.add_argument("--n10k", type=int, default=1)
    p1.add_argument("--n8k", type=int, default=3)
    p1.add_argument("--force", action="store_true")
    sub.add_parser("reembed", help="embed chunks that have no embedding yet (e.g. after changing the model)")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.cmd == "ingest-filings":
        result = asyncio.run(
            cmd_ingest_filings(settings, _split(args.tickers), args.n10k, args.n8k, args.force)
        )
        failed = any(t["error"] or any(d["error"] for d in t["documents"]) for t in result["tickers"])
    else:
        result = asyncio.run(cmd_reembed(settings))
        failed = False
    print(json.dumps(result, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
