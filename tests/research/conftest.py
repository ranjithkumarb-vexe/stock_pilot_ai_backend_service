"""Synthetic-but-realistic 10-K HTML (iXBRL hidden header, TOC table, Items in divs, data tables)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[2] / "evals" / "data"


def sentences(topic: str, n: int, seed: int = 0) -> str:
    base = [
        f"{topic} remained a central focus of management during the fiscal year.",
        f"The Company believes {topic.lower()} will continue to influence results of operations.",
        f"Revenue attributable to {topic.lower()} increased as customers adopted new products.",
        "Competition in our markets is intense and may adversely affect our margins and market share.",
        "We depend on a limited number of suppliers and contract manufacturers for key components.",
        "Changes in laws, regulations and tax rules could increase our costs or restrict our operations.",
    ]
    return " ".join(base[(i + seed) % len(base)] for i in range(n))


def build_10k_html(
    company: str = "Acme Corp", with_injection: str | None = None, thin_item7: bool = False
) -> str:
    risk = sentences("Supply chain concentration", 60, 1)
    if with_injection:
        risk += f" {with_injection} "
    item7 = (
        "The information required by this item is incorporated by reference to the Annual Report."
        if thin_item7
        else sentences("Cloud revenue growth", 90, 2)
    )
    return f"""<html><head><title>{company} 10-K</title><style>p {{color:red}}</style></head><body>
<div style="display:none"><ix:header><ix:hidden>HIDDEN_IXBRL_NOISE_{company}</ix:hidden></ix:header></div>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p><p>FORM 10-K</p><p>{company}</p>
<table><tr><td>Item 1.</td><td>Business</td><td>3</td></tr><tr><td>Item 1A.</td><td>Risk Factors</td><td>9</td></tr>
<tr><td>Item 7.</td><td>Management's Discussion and Analysis</td><td>20</td></tr>
<tr><td>Item 7A.</td><td>Quantitative and Qualitative Disclosures About Market Risk</td><td>30</td></tr></table>
<div><span>PART I</span></div>
<div><b>Item 1. Business</b></div>
<p>{sentences("Platform strategy", 70, 0)}</p>
<p>{company} sells products to consumers and enterprises worldwide.</p>
<table><tr><td></td><td>2025</td><td>2024</td></tr><tr><td>Revenue</td><td>$ 1,200</td><td>$ 1,000</td></tr>
<tr><td>Net income</td><td>$ 200</td><td>$ 150</td></tr></table>
<div><b>Item 1A. Risk Factors</b></div>
<p>{risk}</p>
<div><b>Item 1B. Unresolved Staff Comments</b></div><p>None.</p>
<div><b>Item 7. Management's Discussion and Analysis of Financial Condition</b></div>
<p>{item7}</p>
<p>See Item 1A. Risk Factors for more detail.</p>
<table><tr><td>Segment</td><td>Sales</td><td>Growth</td></tr><tr><td>Cloud</td><td>700</td><td>20%</td></tr><tr><td>Devices</td><td>500</td><td>(3)%</td></tr></table>
<div><b>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</b></div>
<p>{sentences("Interest rate risk", 12, 3)}</p>
<div><b>Item 8. Financial Statements and Supplementary Data</b></div><p>Statements follow.</p>
</body></html>"""


def build_8k_html(items: str = "2.02") -> str:
    return f"""<html><body><p>FORM 8-K</p><p>Item {items} Results of Operations and Financial Condition.</p>
<p>On October 30, 2026 the Company issued a press release announcing results for the quarter.</p>
<table><tr><td>Exhibit</td><td>Description</td></tr><tr><td>99.1</td><td>Press release</td></tr></table></body></html>"""


def load_jsonl(name: str) -> list[dict[str, str]]:
    return [json.loads(line) for line in (EVALS / name).read_text().splitlines() if line.strip()]


@pytest.fixture(scope="session")
def injection_docs() -> list[dict[str, str]]:
    return load_jsonl("injection.jsonl")


@pytest.fixture(scope="session")
def benign_docs() -> list[dict[str, str]]:
    return load_jsonl("benign.jsonl")


# ---------------------------------------------------------------------------- golden data for brief tests
GOLDEN = {"AAPL": "268.70", "JNJ": "171.80", "NVDA": "182.40", "JPM": "301.50", "KO": "69.40"}


async def seed_golden(db: object, symbols: list[str] | None = None, *, chunks: bool = True) -> None:
    """Real recorded XBRL for 5 tickers + synthetic 10-K/8-K text + a last close, in the test database."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from typing import Any, cast

    from app.modules.edgar.universe import BY_SYMBOL
    from app.modules.edgar.xbrl import parse_companyfacts, store_facts
    from app.modules.research.filings import FilingRef
    from app.modules.research.ingest import store_document
    from app.modules.research.pipeline import process_8k_html, process_10k_html
    from tests.edgar.conftest import load_companyfacts_json

    conn = cast(Any, db)
    for sym in symbols or list(GOLDEN):
        u = BY_SYMBOL[sym]
        await conn.execute(
            "insert into tickers (symbol, cik, name, sector, exchange, in_universe) values ($1,$2,$3,$4,$5,true) "
            "on conflict (symbol) do update set name = excluded.name, sector = excluded.sector, cik = excluded.cik, in_universe = true",
            sym, u.cik, u.name, u.sector, u.exchange,
        )  # fmt: skip
        await conn.execute("delete from xbrl_facts where symbol = $1", sym)
        await store_facts(conn, sym, parse_companyfacts(load_companyfacts_json(sym)))
        await conn.execute("delete from prices_daily where symbol = $1", sym)
        await conn.execute(
            "insert into prices_daily (symbol, date, close) values ($1, '2026-10-30', $2)",
            sym,
            Decimal(GOLDEN[sym]),
        )
        await conn.execute("delete from raw_documents where symbol = $1", sym)
        if chunks:
            filings = [
                (FilingRef(u.cik, f"0000000000-26-{sym}1"[:20], "10-K", datetime(2026, 2, 1, tzinfo=UTC).date(), datetime(2025, 12, 31, tzinfo=UTC).date(), "k.htm"), process_10k_html(build_10k_html(u.name))),
                (FilingRef(u.cik, f"0000000000-26-{sym}2"[:20], "8-K", datetime(2026, 10, 13, tzinfo=UTC).date(), None, "e.htm", "2.02,9.01"), process_8k_html(build_8k_html())),
            ]  # fmt: skip
            for ref, doc in filings:
                await store_document(conn, symbol=sym, filing=ref, sha256="a" * 64, storage_path=None, fetched_at=datetime(2026, 10, 31, tzinfo=UTC), processed=doc)  # fmt: skip


async def clean_golden(db: object, symbols: list[str] | None = None) -> None:
    from typing import Any, cast

    conn = cast(Any, db)
    for sym in symbols or list(GOLDEN):
        await conn.execute("delete from raw_documents where symbol = $1", sym)
        await conn.execute("delete from xbrl_facts where symbol = $1", sym)
        await conn.execute("delete from prices_daily where symbol = $1", sym)
