"""companyfacts parsing + `xbrl_facts` storage (idempotent, provenance kept)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from typing import Any

from app.modules.edgar.ingest import TickerRef, ensure_universe_rows, ingest_xbrl_for, universe_refs
from app.modules.edgar.xbrl import CONCEPT_ALLOWLIST, load_facts, parse_companyfacts, store_facts
from tests.edgar.conftest import load_companyfacts_json


def test_parse_keeps_only_allowlisted_concepts_with_provenance() -> None:
    facts = parse_companyfacts(load_companyfacts_json("AAPL"))
    assert len(facts) > 100
    allowed = {f"{t}:{c}" for t, cs in CONCEPT_ALLOWLIST.items() for c in cs}
    assert {f.concept for f in facts} <= allowed
    f = next(
        x
        for x in facts
        if x.concept == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
        and x.period_end == date(2025, 9, 27)
        and x.duration_days is not None
        and x.duration_days > 300
    )
    assert f.value == Decimal(416_161_000_000)
    assert f.accession_no == "0000320193-25-000079" and f.form == "10-K"
    assert f.filed_at.tzinfo is not None


def test_parse_is_defensive() -> None:
    assert parse_companyfacts({}) == []
    assert (
        parse_companyfacts({"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"val": "x"}, {}]}}}}}) == []
    )
    assert parse_companyfacts({"facts": []}) == []


async def test_store_is_idempotent_and_loadable(
    db_admin: Any, ensure_tickers: Callable[..., Awaitable[None]]
) -> None:
    await ensure_tickers(["AAPL"])
    await db_admin.execute("delete from xbrl_facts where symbol = 'AAPL'")
    facts = parse_companyfacts(load_companyfacts_json("AAPL"))
    first = await store_facts(db_admin, "AAPL", facts)
    second = await store_facts(db_admin, "AAPL", facts)
    assert first > 0 and second == 0
    fs = await load_facts(db_admin, "AAPL")
    assert len(fs) == first
    assert fs.latest(["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"], unit="USD") is not None
    early = await load_facts(db_admin, "AAPL", as_of=date(2000, 1, 1))
    assert len(early) == 0
    await db_admin.execute("delete from xbrl_facts where symbol = 'AAPL'")


async def test_ensure_universe_rows_never_overwrites_the_seed(db_admin: Any) -> None:
    await db_admin.execute("delete from tickers where symbol = 'KO'")
    await db_admin.execute("insert into tickers (symbol, name, sector) values ('KO', 'Seeded Name', 'S')")
    await ensure_universe_rows(db_admin, ["KO", "NOPE"])
    row = await db_admin.fetchrow("select name, cik, sector from tickers where symbol = 'KO'")
    assert row["name"] == "Seeded Name" and row["cik"] == "21344" and row["sector"] == "S"
    assert await db_admin.fetchval("select count(*) from tickers where symbol = 'NOPE'") == 0
    refs = await universe_refs(db_admin, ["KO"])
    assert refs == [TickerRef("KO", "21344")]
    await db_admin.execute("delete from tickers where symbol = 'KO'")


async def test_ingest_for_uses_client_and_reports_errors(
    db_admin: Any, ensure_tickers: Callable[..., Awaitable[None]], tmp_path: Any
) -> None:
    import json

    import httpx

    from app.core.clock import FakeClock
    from app.modules.edgar.client import EdgarClient

    await ensure_tickers(["JNJ"])
    await db_admin.execute("delete from xbrl_facts where symbol = 'JNJ'")
    body = json.dumps(load_companyfacts_json("JNJ")).encode()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body) if "0000200406" in str(req.url) else httpx.Response(404)

    async def public(_h: str) -> list[str]:
        return ["104.18.32.7"]

    async with EdgarClient(
        "StockPilot t@example.com",
        clock=FakeClock(),
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        resolver=public,
    ) as client:
        ok = await ingest_xbrl_for(db_admin, client, TickerRef("JNJ", "200406"))
        again = await ingest_xbrl_for(db_admin, client, TickerRef("JNJ", "200406"))
        bad = await ingest_xbrl_for(db_admin, client, TickerRef("JNJ", "999"))
    assert ok.error is None and ok.inserted == ok.parsed > 0 and not ok.from_cache
    assert again.inserted == 0 and again.from_cache
    assert bad.error is not None and "NotFound" in bad.error
    await db_admin.execute("delete from xbrl_facts where symbol = 'JNJ'")
