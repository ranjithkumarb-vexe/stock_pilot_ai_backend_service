"""Numeric ledger (SPEC 7.5): every figure in a brief is computed here, in code, from `xbrl_facts` and prices.

Ids are stable (m1..m12) whether or not a value exists: an unavailable figure keeps its id with display "n/m",
so templates never reference a missing id. Dates/labels are not numbers and never enter the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.core.db import Connection
from app.core.pricesource import PriceSource, QuoteUnavailableError
from app.modules.edgar.ratios import (
    Metric,
    compute_core_metrics,
    fiscal_year_label,
    ordinal,
    percentile_rank,
)
from app.modules.edgar.xbrl import ANNUAL_FORMS, FactSet, load_facts

NM = "n/m"

#: stable id assignment: (ledger key, label)
LEDGER_SPEC: tuple[tuple[str, str], ...] = (
    ("revenue", "Revenue, latest fiscal year"),
    ("revenue_growth_pct", "Revenue growth"),
    ("operating_margin_pct", "Operating margin"),
    ("fcf", "Free cash flow"),
    ("debt_to_equity", "Debt / equity"),
    ("eps_diluted", "Diluted EPS (last fiscal year)"),
    ("price", "Price"),
    ("pe", "P/E"),
    ("dividend_yield_pct", "Dividend yield"),
    ("pe_percentile", "P/E percentile vs peers"),
    ("growth_percentile", "Revenue growth percentile vs peers"),
    ("margin_percentile", "Operating margin percentile vs peers"),
)
KEY_TO_ID = {k: f"m{i + 1}" for i, (k, _) in enumerate(LEDGER_SPEC)}


def fmt_money(v: Decimal) -> str:
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= Decimal(10) ** 12:
        return f"{sign}${a / Decimal(10) ** 12:,.2f}T"
    if a >= Decimal(10) ** 9:
        return f"{sign}${a / Decimal(10) ** 9:,.1f}B"
    if a >= Decimal(10) ** 6:
        return f"{sign}${a / Decimal(10) ** 6:,.1f}M"
    return f"{sign}${a:,.0f}"


def display_for(key: str, value: Decimal | None) -> str:
    if value is None:
        return NM
    if key in ("revenue", "fcf"):
        return fmt_money(value)
    if key == "revenue_growth_pct":
        return f"{value:+.1f}%"
    if key == "dividend_yield_pct" and 0 < value < Decimal("0.1"):
        return f"{value:.2f}%"
    if key in ("operating_margin_pct", "dividend_yield_pct"):
        return f"{value:.1f}%"
    if key in ("debt_to_equity",):
        return f"{value:.2f}x"
    if key == "pe":
        return f"{value:.1f}x"
    if key in ("eps_diluted", "price"):
        return f"${value:,.2f}"
    if key.endswith("_percentile"):
        return ordinal(round(float(value)))
    return str(value)


@dataclass(slots=True)
class LedgerEntry:
    id: str
    key: str
    label: str
    value: Decimal | None
    display: str
    unit: str
    source: str
    kind: str  # xbrl | code | price
    accession_nos: tuple[str, ...] = ()
    period_end: date | None = None
    note: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": None if self.value is None else float(self.value),
            "display": self.display,
            "unit": self.unit,
            "source": self.source,
            "kind": self.kind,
            "accession_nos": list(self.accession_nos),
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "note": self.note,
        }


@dataclass(slots=True)
class Ledger:
    symbol: str
    name: str
    sector: str | None
    as_of: date
    fy_label: str
    entries: dict[str, LedgerEntry] = field(default_factory=dict)  # by id
    price_quote_note: str | None = None
    price_freshness: str | None = None

    def by_key(self, key: str) -> LedgerEntry:
        return self.entries[KEY_TO_ID[key]]

    def get(self, ledger_id: str) -> LedgerEntry | None:
        return self.entries.get(ledger_id)

    def as_json(self) -> dict[str, Any]:
        return {i: e.as_json() for i, e in self.entries.items()}


_UNIT = {
    "revenue": "usd", "revenue_growth_pct": "pct", "operating_margin_pct": "pct", "fcf": "usd",
    "debt_to_equity": "x", "eps_diluted": "usd_per_share", "price": "usd_per_share", "pe": "x",
    "dividend_yield_pct": "pct", "pe_percentile": "percentile", "growth_percentile": "percentile",
    "margin_percentile": "percentile",
}  # fmt: skip


async def last_close(conn: Connection, symbol: str, as_of: date) -> tuple[Decimal, date] | None:
    row = await conn.fetchrow(
        "select close, date from prices_daily where symbol = $1 and date <= $2 order by date desc limit 1",
        symbol,
        as_of,
    )
    return (row["close"], row["date"]) if row else None


async def resolve_price(
    conn: Connection, symbol: str, as_of: date, price_source: PriceSource | None
) -> tuple[Decimal | None, str, str | None]:
    """(price, source label, freshness). Live quote first, `prices_daily` close as fallback."""
    if price_source is not None:
        try:
            q = await price_source.get_last(symbol)
            return q.price, f"{q.source} · {q.freshness}", q.freshness
        except QuoteUnavailableError:
            pass
    lc = await last_close(conn, symbol, as_of)
    if lc is not None:
        return lc[0], f"prices_daily close {lc[1].isoformat()}", "closed"
    return None, "no price available", None


async def peer_stats(
    conn: Connection, symbol: str, sector: str | None, as_of: date
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """(pe, growth, margin) of the peer set INCLUDING the company: same sector if >= 3 members else the universe."""
    rows = await conn.fetch("select symbol, sector from tickers where in_universe order by symbol")
    same = [r["symbol"] for r in rows if r["sector"] == sector]
    peers = same if len(same) >= 3 else [r["symbol"] for r in rows]
    if symbol not in peers:
        peers.append(symbol)
    pes: list[Decimal | None] = []
    growth: list[Decimal | None] = []
    margin: list[Decimal | None] = []
    for p in peers:
        fs = await load_facts(conn, p, as_of=as_of, forms=ANNUAL_FORMS)
        if len(fs) == 0:
            continue
        lc = await last_close(conn, p, as_of)
        sec = next((r["sector"] for r in rows if r["symbol"] == p), None)
        m = compute_core_metrics(fs, sector=sec, price=lc[0] if lc else None, as_of=as_of)
        pes.append(m["pe"].value)
        growth.append(m["revenue_growth_pct"].value)
        margin.append(m["operating_margin_pct"].value)
    return pes, growth, margin


async def build_ledger(
    conn: Connection, symbol: str, as_of: date, price_source: PriceSource | None = None
) -> tuple[Ledger, FactSet]:
    trow = await conn.fetchrow("select name, sector from tickers where symbol = $1", symbol)
    name = trow["name"] if trow else symbol
    sector = trow["sector"] if trow else None
    fs = await load_facts(conn, symbol, as_of=as_of, forms=ANNUAL_FORMS)
    price, price_src, freshness = await resolve_price(conn, symbol, as_of, price_source)
    metrics = compute_core_metrics(fs, sector=sector, price=price, as_of=as_of)
    pes, growth, margin = await peer_stats(conn, symbol, sector, as_of)

    values: dict[str, Metric | None] = {k: metrics.get(k) for k, _ in LEDGER_SPEC}
    derived: dict[str, Decimal | None] = {
        "pe_percentile": percentile_rank(metrics["pe"].value, pes),
        "growth_percentile": percentile_rank(metrics["revenue_growth_pct"].value, growth),
        "margin_percentile": percentile_rank(metrics["operating_margin_pct"].value, margin),
    }
    fy = fiscal_year_label(metrics["revenue"].period_end)
    ledger = Ledger(
        symbol=symbol, name=name, sector=sector, as_of=as_of, fy_label=fy, price_freshness=freshness
    )
    for key, label in LEDGER_SPEC:
        m = values.get(key)
        accs: tuple[str, ...]
        pe_: date | None
        if key == "price":
            value, source, kind, accs, pe_ = price, price_src, "price", (), None
            note = None
        elif key in derived:
            value = derived[key]
            src_metric = {
                "pe_percentile": "pe",
                "growth_percentile": "revenue_growth_pct",
                "margin_percentile": "operating_margin_pct",
            }[key]
            source = f"code: percentile of {src_metric} within peer set"
            kind, accs, pe_, note = "code", (), metrics[src_metric].period_end, None
        else:
            assert m is not None
            value, source, accs, pe_, note = m.value, m.source, m.accession_nos, m.period_end, m.note
            kind = "xbrl" if source.startswith("us-gaap:") else "code"
        eid = KEY_TO_ID[key]
        ledger.entries[eid] = LedgerEntry(
            id=eid, key=key, label=label, value=value, display=display_for(key, value), unit=_UNIT[key],
            source=source, kind=kind, accession_nos=tuple(accs), period_end=pe_, note=note,
        )  # fmt: skip
    return ledger, fs
