"""Filing-metric tool: reads XBRL facts / ratio library (code), never the model. Used by chat and the evals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.db import Connection
from app.core.pricesource import PriceSource
from app.modules.chat.ledger import ChatLedger
from app.modules.chat.symbols import Company
from app.modules.edgar import ratios as r
from app.modules.edgar.client import companyfacts_url
from app.modules.edgar.xbrl import ANNUAL_FORMS, Fact, FactSet, load_facts
from app.modules.research.brief.ledger import display_for, fmt_money, resolve_price

#: key -> (label, matching phrases). Order matters: the first metric whose phrase matches is picked first.
METRIC_PHRASES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "operating_cash_flow",
        "Net cash from operating activities",
        re.compile(r"operating (?:cash|activities)|cash (?:flow )?(?:from|provided)|\bocf\b", re.I),
    ),
    ("fcf", "Free cash flow", re.compile(r"free cash ?flow|\bfcf\b", re.I)),
    (
        "operating_margin_pct",
        "Operating margin",
        re.compile(r"operating margin|margins?\b|profitab", re.I),
    ),
    (
        "operating_income",
        "Operating income",
        re.compile(r"operating income|pre-?tax income|income before", re.I),
    ),
    (
        "revenue_growth_pct",
        "Revenue growth",
        re.compile(r"(?:revenue|sales) growth|growth (?:rate )?in (?:revenue|sales)|grow(?:n|th|ing)", re.I),
    ),
    (
        "eps_diluted",
        "Diluted EPS",
        re.compile(r"earnings per share|\beps\b|per[- ]share earnings", re.I),
    ),
    ("net_income", "Net income", re.compile(r"net (?:income|earnings|profit)|\bprofit\b|bottom line", re.I)),
    (
        "equity",
        "Stockholders' equity",
        re.compile(r"(?:stock|share)holders'? equity|\bequity\b|book value", re.I),
    ),
    (
        "debt_to_equity",
        "Debt / equity",
        re.compile(r"debt[- ]to[- ]equity|debt / equity|leverage|\bd/e\b|\bdebt\b", re.I),
    ),
    ("pe", "P/E", re.compile(r"\bp/?e\b|price[- ]to[- ]earnings|earnings multiple", re.I)),
    ("dividend_yield_pct", "Dividend yield", re.compile(r"dividend", re.I)),
    ("revenue", "Revenue", re.compile(r"revenues?|\bsales\b|top[- ]line", re.I)),
)
METRIC_KEYS = tuple(k for k, _, _ in METRIC_PHRASES)


async def load_annual_facts(conn: Connection, symbol: str, as_of: date) -> FactSet:
    """XBRL facts reported in annual filings only (10-K family), filed on/before `as_of`.

    `companyfacts` also carries facts tagged to proxy statements (DEF 14A) and quarterly reports; with
    "latest filing wins" a proxy restatement can replace the 10-K value with a rounded one (seen live: JPM net
    income 57,000,000,000 from a DEF 14A vs 57,048,000,000 in the 10-K), so chat answers read 10-K facts only.
    """
    return await load_facts(conn, symbol, as_of=as_of, forms=ANNUAL_FORMS)


def metrics_in(message: str) -> list[str]:
    """Metric keys named in the message, in priority order (at most 3)."""
    found = [k for k, _, rx in METRIC_PHRASES if rx.search(message)]
    if "revenue_growth_pct" in found and "revenue" in found:
        found.remove("revenue")
    if "net_income" in found and "eps_diluted" in found:
        found.remove("net_income")
    if "operating_margin_pct" in found and "operating_income" in found:
        found.remove("operating_income")
    return found[:3]


@dataclass(frozen=True, slots=True)
class MetricResult:
    key: str
    label: str
    symbol: str
    value: Decimal | None
    display: str
    exact: str | None
    period_end: date | None
    source: str
    accession_nos: tuple[str, ...]
    note: str | None = None


def _exact_usd(v: Decimal) -> str:
    return f"{v:,.0f} USD"


def _from_fact(key: str, label: str, symbol: str, fact: Fact | None, unit: str) -> MetricResult:
    if fact is None:
        return MetricResult(key, label, symbol, None, "n/m", None, None, "us-gaap", ())
    if unit == "usd_per_share":
        disp, exact = f"${fact.value:,.2f}", None
    else:
        disp, exact = fmt_money(fact.value), _exact_usd(fact.value)
    return MetricResult(
        key, label, symbol, fact.value, disp, exact, fact.period_end, fact.concept, (fact.accession_no,)
    )


def _from_metric(key: str, label: str, symbol: str, m: r.Metric) -> MetricResult:
    exact = _exact_usd(m.value) if m.value is not None and m.unit == "usd" else None
    return MetricResult(
        key, label, symbol, m.value, display_for(key, m.value), exact, m.period_end, m.source,
        m.accession_nos, m.note,
    )  # fmt: skip


async def get_metric(
    conn: Connection,
    company: Company,
    key: str,
    as_of: date,
    price_source: PriceSource | None = None,
    *,
    facts: FactSet | None = None,
) -> MetricResult:
    """One metric for one company, point-in-time as of `as_of`."""
    fs = facts if facts is not None else await load_annual_facts(conn, company.symbol, as_of)
    label = next(lbl for k, lbl, _ in METRIC_PHRASES if k == key)
    sym = company.symbol
    if key == "net_income":
        return _from_fact(
            key, label, sym, fs.latest(r.NET_INCOME, kind="annual", as_of=as_of, unit="USD"), "usd"
        )
    if key == "operating_cash_flow":
        return _from_fact(key, label, sym, fs.latest(r.OCF, kind="annual", as_of=as_of, unit="USD"), "usd")
    if key == "equity":
        return _from_fact(
            key, label, sym, fs.latest(r.EQUITY, kind="instant", as_of=as_of, unit="USD"), "usd"
        )
    if key == "operating_income":
        aliases = [*r.OPERATING_INCOME, *r.PRETAX_INCOME]
        return _from_fact(key, label, sym, fs.latest(aliases, kind="annual", as_of=as_of, unit="USD"), "usd")
    if key == "eps_diluted":
        return _from_metric(key, label, sym, r.eps_diluted(fs, as_of=as_of))
    price: Decimal | None = None
    if key in ("pe", "dividend_yield_pct"):
        price = (await resolve_price(conn, sym, as_of, price_source))[0]
    core = r.compute_core_metrics(fs, sector=company.sector, price=price, as_of=as_of)
    return _from_metric(key, label, sym, core[key])


def _period_end_of(fs: FactSet, as_of: date) -> date | None:
    f = fs.latest(r.NET_INCOME, kind="annual", as_of=as_of, unit="USD")
    return f.period_end if f else None


def register(ledger: ChatLedger, res: MetricResult, fy: str) -> str:
    """Add a metric result to the ledger; returns the display text to use in the answer."""
    return ledger.add(
        f"{res.symbol} {res.label} ({fy})", res.display, source=res.source, value=res.value, exact=res.exact
    )


def companyfacts_link(cik: str | None) -> str | None:
    return companyfacts_url(cik) if cik else None
