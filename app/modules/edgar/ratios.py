"""Ratio library (pure functions over a `FactSet`). Every figure shown in a brief is produced here.

Conventions
* Percentages are percent numbers (6.8 == 6.8%). Money is USD. Missing inputs -> `value=None` (never raises,
  never guesses); "not meaningful" cases (banks' free cash flow / debt-to-equity) -> `value=None` + `note`.
* `as_of` is the look-ahead guard: only facts *filed on or before* that date are used.
* Source strings match the design's citation drawer, e.g. `us-gaap:Revenues` or
  `code: OperatingIncomeLoss / Revenues`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from app.modules.edgar.xbrl import Fact, FactSet

Unit = Literal["usd", "pct", "x", "usd_per_share"]

REVENUE = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:RevenuesNetOfInterestExpense",
)
OPERATING_INCOME = ("us-gaap:OperatingIncomeLoss",)
PRETAX_INCOME = (
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)
NET_INCOME = ("us-gaap:NetIncomeLoss",)
OCF = ("us-gaap:NetCashProvidedByUsedInOperatingActivities",)
CAPEX = ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:PaymentsToAcquireProductiveAssets")
EQUITY = (
    "us-gaap:StockholdersEquity",
    "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
EPS_DILUTED = ("us-gaap:EarningsPerShareDiluted",)
DPS = ("us-gaap:CommonStockDividendsPerShareDeclared", "us-gaap:CommonStockDividendsPerShareCashPaid")
LTD_TOTAL = (
    "us-gaap:LongTermDebt",
    "us-gaap:LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
)
LTD_NONCURRENT = ("us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligations")
LTD_CURRENT = (
    "us-gaap:LongTermDebtCurrent",
    "us-gaap:DebtCurrent",
    "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
)
COMMERCIAL_PAPER = ("us-gaap:CommercialPaper",)
CASH = ("us-gaap:CashAndCashEquivalentsAtCarryingValue",)

#: sectors for which FCF and debt/equity are not meaningful (design shows "n/m" for JPM)
NOT_MEANINGFUL_SECTORS = frozenset({"Financials"})
BILLION = Decimal(1_000_000_000)


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    label: str
    value: Decimal | None
    unit: Unit
    source: str
    period_end: date | None = None
    inputs: tuple[Fact, ...] = field(default_factory=tuple)
    note: str | None = None
    price: Decimal | None = None

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def accession_nos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.accession_no for f in self.inputs))


def _none(key: str, label: str, unit: Unit, source: str, note: str | None = None) -> Metric:
    return Metric(key=key, label=label, value=None, unit=unit, source=source, note=note)


def fiscal_year_label(period_end: date | None) -> str:
    return f"FY{period_end.year}" if period_end else "latest fiscal year"


def _latest_revenue(fs: FactSet, as_of: date | None) -> Fact | None:
    return fs.latest(REVENUE, kind="annual", as_of=as_of, unit="USD")


def revenue(fs: FactSet, *, as_of: date | None = None) -> Metric:
    fact = _latest_revenue(fs, as_of)
    if fact is None:
        return _none("revenue", "Revenue, latest fiscal year", "usd", "us-gaap:Revenues")
    return Metric(
        "revenue", "Revenue, latest fiscal year", fact.value, "usd", fact.concept, fact.period_end, (fact,)
    )


def revenue_growth_pct(fs: FactSet, *, as_of: date | None = None) -> Metric:
    src = "code: Revenues(t) / Revenues(t-1) - 1"
    cur = _latest_revenue(fs, as_of)
    if cur is None:
        return _none("revenue_growth_pct", "Revenue growth", "pct", src)
    prev = fs.prior_year(REVENUE, cur, as_of=as_of, unit="USD")
    if prev is None or prev.value == 0:
        return Metric("revenue_growth_pct", "Revenue growth", None, "pct", src, cur.period_end, (cur,))
    return Metric(
        "revenue_growth_pct",
        "Revenue growth",
        (cur.value / prev.value - 1) * 100,
        "pct",
        src,
        cur.period_end,
        (cur, prev),
    )


def operating_margin_pct(fs: FactSet, *, as_of: date | None = None) -> Metric:
    src = "code: OperatingIncomeLoss / Revenues"
    rev = _latest_revenue(fs, as_of)
    if rev is None or rev.value == 0:
        return _none("operating_margin_pct", "Operating margin", "pct", src)
    op = fs.at(OPERATING_INCOME, rev.period_end, as_of=as_of, unit="USD")
    note = None
    if op is None:  # banks do not report OperatingIncomeLoss: use pre-tax income and say so
        op = fs.at(PRETAX_INCOME, rev.period_end, as_of=as_of, unit="USD")
        if op is None:
            return Metric(
                "operating_margin_pct", "Operating margin", None, "pct", src, rev.period_end, (rev,)
            )
        src = "code: IncomeLossBeforeIncomeTaxes / Revenues"
        note = "Pre-tax income used because the company reports no operating income line"
    return Metric(
        "operating_margin_pct",
        "Operating margin",
        op.value / rev.value * 100,
        "pct",
        src,
        rev.period_end,
        (op, rev),
        note,
    )


def free_cash_flow(fs: FactSet, *, sector: str | None = None, as_of: date | None = None) -> Metric:
    src = "code: NetCashProvidedByOperatingActivities - PaymentsToAcquirePPE"
    if sector in NOT_MEANINGFUL_SECTORS:
        return _none("fcf", "Free cash flow", "usd", src, "Not meaningful for banks")
    ocf = fs.latest(OCF, kind="annual", as_of=as_of, unit="USD")
    if ocf is None:
        return _none("fcf", "Free cash flow", "usd", src)
    capex = fs.at(CAPEX, ocf.period_end, as_of=as_of, unit="USD")
    if capex is None:
        return Metric("fcf", "Free cash flow", None, "usd", src, ocf.period_end, (ocf,))
    return Metric("fcf", "Free cash flow", ocf.value - capex.value, "usd", src, ocf.period_end, (ocf, capex))


def _total_debt(fs: FactSet, period_end: date, as_of: date | None) -> tuple[Decimal, tuple[Fact, ...]] | None:
    def get(aliases: Sequence[str]) -> Fact | None:
        return fs.at(aliases, period_end, kind="instant", as_of=as_of, unit="USD")

    total = get(LTD_TOTAL)
    cp = get(COMMERCIAL_PAPER)
    if total is not None:
        parts = [f for f in (total, cp) if f is not None]
    else:
        noncurrent = get(LTD_NONCURRENT)
        current = get(LTD_CURRENT)
        if noncurrent is None and current is None:
            return None
        parts = [f for f in (noncurrent, current, cp) if f is not None]
    return sum((f.value for f in parts), Decimal(0)), tuple(parts)


def debt_to_equity(fs: FactSet, *, sector: str | None = None, as_of: date | None = None) -> Metric:
    src = "code: Debt / StockholdersEquity"
    if sector in NOT_MEANINGFUL_SECTORS:
        return _none("debt_to_equity", "Debt / equity", "x", src, "Not meaningful for banks")
    rev = _latest_revenue(fs, as_of)
    if rev is None:
        return _none("debt_to_equity", "Debt / equity", "x", src)
    eq = fs.at(EQUITY, rev.period_end, kind="instant", as_of=as_of, unit="USD")
    debt = _total_debt(fs, rev.period_end, as_of)
    if eq is None or eq.value <= 0 or debt is None:
        return Metric("debt_to_equity", "Debt / equity", None, "x", src, rev.period_end)
    return Metric(
        "debt_to_equity", "Debt / equity", debt[0] / eq.value, "x", src, rev.period_end, (*debt[1], eq)
    )


def eps_diluted(fs: FactSet, *, as_of: date | None = None) -> Metric:
    src = "us-gaap:EarningsPerShareDiluted"
    fact = fs.latest(EPS_DILUTED, kind="annual", as_of=as_of, unit="USD/shares")
    if fact is None:
        return _none("eps_diluted", "Diluted EPS (last fiscal year)", "usd_per_share", src)
    return Metric(
        "eps_diluted",
        "Diluted EPS (last fiscal year)",
        fact.value,
        "usd_per_share",
        fact.concept,
        fact.period_end,
        (fact,),
    )


def pe_ratio(fs: FactSet, *, price: Decimal | None, as_of: date | None = None) -> Metric:
    src = "code: price / EPS"
    eps = eps_diluted(fs, as_of=as_of)
    if price is None or eps.value is None:
        return _none("pe", "P/E", "x", src)
    if eps.value <= 0:
        return Metric("pe", "P/E", None, "x", src, eps.period_end, eps.inputs, "Negative earnings", price)
    return Metric("pe", "P/E", price / eps.value, "x", src, eps.period_end, eps.inputs, None, price)


def dividend_yield_pct(fs: FactSet, *, price: Decimal | None, as_of: date | None = None) -> Metric:
    src = "code: DividendsDeclaredPerShare / price"
    dps = fs.latest(DPS, kind="annual", as_of=as_of, unit="USD/shares")
    if price is None or price <= 0:
        return _none("dividend_yield_pct", "Dividend yield", "pct", src)
    if dps is None:  # no dividend declared: a real zero for non-payers
        rev = _latest_revenue(fs, as_of)
        if rev is None:
            return _none("dividend_yield_pct", "Dividend yield", "pct", src)
        return Metric(
            "dividend_yield_pct",
            "Dividend yield",
            Decimal(0),
            "pct",
            src,
            rev.period_end,
            (),
            "No dividend per share reported",
            price,
        )
    return Metric(
        "dividend_yield_pct",
        "Dividend yield",
        dps.value / price * 100,
        "pct",
        src,
        dps.period_end,
        (dps,),
        None,
        price,
    )


def percentile_rank(value: Decimal | None, population: Sequence[Decimal | None]) -> Decimal | None:
    """Mid-rank percentile (0-100) of `value` within `population` (which should include the company itself).

    `None` entries are ignored; an empty population or a missing value yields `None`.
    """
    if value is None:
        return None
    vals = [p for p in population if p is not None]
    if not vals:
        return None
    below = sum(1 for p in vals if p < value)
    equal = sum(1 for p in vals if p == value)
    return (Decimal(below) + Decimal(equal) / 2) / Decimal(len(vals)) * 100


def compute_core_metrics(
    fs: FactSet, *, sector: str | None, price: Decimal | None, as_of: date | None = None
) -> dict[str, Metric]:
    """All figures a brief needs, keyed by metric key (stable order)."""
    metrics = [
        revenue(fs, as_of=as_of),
        revenue_growth_pct(fs, as_of=as_of),
        operating_margin_pct(fs, as_of=as_of),
        free_cash_flow(fs, sector=sector, as_of=as_of),
        debt_to_equity(fs, sector=sector, as_of=as_of),
        eps_diluted(fs, as_of=as_of),
        pe_ratio(fs, price=price, as_of=as_of),
        dividend_yield_pct(fs, price=price, as_of=as_of),
    ]
    return {m.key: m for m in metrics}


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
