"""XBRL `companyfacts` parsing, storage into `xbrl_facts`, and an in-memory `FactSet` for ratio code.

The SEC file holds every fact a company ever reported (megabytes). We keep a curated concept allow-list
(everything the ratio library and the brief need) so the table stays small and the ledger is auditable.
Values are stored exactly as reported (`numeric`), with provenance `accession_no / form / filed_at`.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.db import Connection

US_GAAP = "us-gaap"
DEI = "dei"

#: concept names per taxonomy that are ingested
CONCEPT_ALLOWLIST: dict[str, frozenset[str]] = {
    US_GAAP: frozenset(
        {
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "RevenuesNetOfInterestExpense",
            "GrossProfit",
            "CostOfRevenue",
            "OperatingIncomeLoss",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            "NetIncomeLoss",
            "EarningsPerShareDiluted",
            "EarningsPerShareBasic",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "NetCashProvidedByUsedInOperatingActivities",
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "DepreciationDepletionAndAmortization",
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "Assets",
            "AssetsCurrent",
            "Liabilities",
            "LiabilitiesCurrent",
            "CashAndCashEquivalentsAtCarryingValue",
            "MarketableSecuritiesCurrent",
            "LongTermDebt",
            "LongTermDebtNoncurrent",
            "LongTermDebtCurrent",
            "LongTermDebtAndCapitalLeaseObligations",
            "LongTermDebtAndCapitalLeaseObligationsCurrent",
            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
            "DebtCurrent",
            "ShortTermBorrowings",
            "CommercialPaper",
            "CommonStockDividendsPerShareDeclared",
            "CommonStockDividendsPerShareCashPaid",
            "PaymentsOfDividends",
            "PaymentsOfDividendsCommonStock",
            "PaymentsForRepurchaseOfCommonStock",
            "InterestExpense",
        }
    ),
    DEI: frozenset({"EntityCommonStockSharesOutstanding", "EntityPublicFloat"}),
}


@dataclass(frozen=True, slots=True)
class Fact:
    concept: str  # "us-gaap:Revenues"
    unit: str
    period_start: date | None
    period_end: date
    value: Decimal
    fy: int | None
    fp: str | None
    accession_no: str
    form: str | None
    filed_at: datetime

    @property
    def filed_date(self) -> date:
        return self.filed_at.date()

    @property
    def duration_days(self) -> int | None:
        return None if self.period_start is None else (self.period_end - self.period_start).days


def _parse_date(raw: object) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(str(raw))


def parse_companyfacts(payload: dict[str, Any]) -> list[Fact]:
    """Flatten a `companyfacts` document to allow-listed `Fact`s (malformed entries are skipped)."""
    out: list[Fact] = []
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return out
    for taxonomy, allowed in CONCEPT_ALLOWLIST.items():
        concepts = facts.get(taxonomy)
        if not isinstance(concepts, dict):
            continue
        for name in sorted(allowed & concepts.keys()):
            units = concepts[name].get("units", {})
            for unit, entries in units.items():
                for e in entries:
                    try:
                        end = _parse_date(e["end"])
                        filed = _parse_date(e["filed"])
                        if end is None or filed is None:
                            continue
                        out.append(
                            Fact(
                                concept=f"{taxonomy}:{name}",
                                unit=str(unit),
                                period_start=_parse_date(e.get("start")),
                                period_end=end,
                                value=Decimal(str(e["val"])),
                                fy=int(e["fy"]) if e.get("fy") is not None else None,
                                fp=e.get("fp"),
                                accession_no=str(e["accn"]),
                                form=e.get("form"),
                                filed_at=datetime(filed.year, filed.month, filed.day, tzinfo=UTC),
                            )
                        )
                    except (KeyError, ValueError, InvalidOperation, TypeError):
                        continue
    return out


_INSERT_SQL = """
with ins as (
    insert into xbrl_facts (symbol, concept, unit, period_start, period_end, fy, fp, value, accession_no, form, filed_at)
    select $1, c, u, ps, pe, fy, fp, v, a, f, fa
      from unnest($2::text[], $3::text[], $4::date[], $5::date[], $6::int[], $7::text[], $8::numeric[],
                  $9::text[], $10::text[], $11::timestamptz[])
           as t(c, u, ps, pe, fy, fp, v, a, f, fa)
    on conflict do nothing
    returning 1
)
select count(*) from ins
"""


async def store_facts(conn: Connection, symbol: str, facts: Sequence[Fact], batch: int = 5000) -> int:
    """Insert facts (idempotent: natural-key conflicts are skipped). Returns rows actually inserted."""
    inserted = 0
    for i in range(0, len(facts), batch):
        chunk = facts[i : i + batch]
        n = await conn.fetchval(
            _INSERT_SQL,
            symbol,
            [f.concept for f in chunk],
            [f.unit for f in chunk],
            [f.period_start for f in chunk],
            [f.period_end for f in chunk],
            [f.fy for f in chunk],
            [f.fp for f in chunk],
            [f.value for f in chunk],
            [f.accession_no for f in chunk],
            [f.form for f in chunk],
            [f.filed_at for f in chunk],
        )
        inserted += int(n)
    return inserted


async def load_facts(
    conn: Connection,
    symbol: str,
    *,
    as_of: date | None = None,
    forms: Collection[str] | None = None,
) -> FactSet:
    """Read a ticker's facts (optionally only those filed on/before `as_of`, and only from the given `forms`).

    `companyfacts` also carries facts tagged to proxy statements (DEF 14A) and quarterly reports; "latest filing
    wins" lets a rounded proxy restatement replace the 10-K value (CR-17), so annual consumers pass
    `forms=ANNUAL_FORMS`.
    """
    rows = await conn.fetch(
        "select concept, unit, period_start, period_end, fy, fp, value, accession_no, form, filed_at "
        "from xbrl_facts where symbol = $1 and ($2::date is null or filed_at::date <= $2::date) "
        "and ($3::text[] is null or form = any($3::text[]))",
        symbol,
        as_of,
        sorted(forms) if forms is not None else None,
    )
    return FactSet(
        Fact(
            concept=r["concept"],
            unit=r["unit"],
            period_start=r["period_start"],
            period_end=r["period_end"],
            value=r["value"],
            fy=r["fy"],
            fp=r["fp"],
            accession_no=r["accession_no"],
            form=r["form"],
            filed_at=r["filed_at"],
        )
        for r in rows
    )


ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "20-F", "40-F"})


class FactSet:
    """Immutable in-memory view over one company's facts with restatement- and look-ahead-aware selectors."""

    def __init__(self, facts: Iterable[Fact]) -> None:
        self._facts = tuple(facts)

    def __len__(self) -> int:
        return len(self._facts)

    def _select(
        self,
        concept: str,
        kind: str,
        as_of: date | None,
        unit: str | None,
    ) -> list[Fact]:
        picked: dict[date, Fact] = {}
        for f in self._facts:
            if f.concept != concept:
                continue
            if unit is not None and f.unit != unit:
                continue
            if as_of is not None and f.filed_date > as_of:
                continue
            if kind == "annual":
                d = f.duration_days
                if d is None or not ANNUAL_MIN_DAYS <= d <= ANNUAL_MAX_DAYS:
                    continue
            elif kind == "instant":
                if f.period_start is not None:
                    continue
            else:
                raise ValueError(kind)
            cur = picked.get(f.period_end)
            # latest filing wins (restatements); tie-break on accession for determinism
            if cur is None or (f.filed_at, f.accession_no) > (cur.filed_at, cur.accession_no):
                picked[f.period_end] = f
        return sorted(picked.values(), key=lambda f: f.period_end, reverse=True)

    def series(
        self,
        aliases: Sequence[str],
        *,
        kind: str = "annual",
        as_of: date | None = None,
        unit: str | None = None,
    ) -> list[Fact]:
        """Newest-first series for the first alias that reaches the most recent period.

        Companies switch revenue concepts over time (SalesRevenueNet -> RevenueFromContract...); picking the
        alias with the newest data (alias order breaks ties) keeps the series current and self-consistent.
        """
        best: list[Fact] = []
        for alias in aliases:
            cand = self._select(alias, kind, as_of, unit)
            if cand and (not best or cand[0].period_end > best[0].period_end):
                best = cand
        return best

    def latest(
        self,
        aliases: Sequence[str],
        *,
        kind: str = "annual",
        as_of: date | None = None,
        unit: str | None = None,
    ) -> Fact | None:
        s = self.series(aliases, kind=kind, as_of=as_of, unit=unit)
        return s[0] if s else None

    def at(
        self,
        aliases: Sequence[str],
        period_end: date,
        *,
        kind: str = "annual",
        as_of: date | None = None,
        unit: str | None = None,
        tolerance_days: int = 7,
    ) -> Fact | None:
        """Fact whose period ends within `tolerance_days` of `period_end` (any alias, first match)."""
        for alias in aliases:
            for f in self._select(alias, kind, as_of, unit):
                if abs((f.period_end - period_end).days) <= tolerance_days:
                    return f
        return None

    def prior_year(
        self,
        aliases: Sequence[str],
        current: Fact,
        *,
        as_of: date | None = None,
        unit: str | None = None,
    ) -> Fact | None:
        target = current.period_end - timedelta(days=365)
        return self.at(aliases, target, kind="annual", as_of=as_of, unit=unit, tolerance_days=20)
