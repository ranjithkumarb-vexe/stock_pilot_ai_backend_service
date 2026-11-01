"""Ratio library vs recorded real filings (AAPL FY2025, JNJ FY2025, NVDA FY2026 10-Ks).

Expected values are the figures printed in those 10-Ks (revenue, prior-year revenue, operating income,
operating cash flow, capex), hand-combined here so the test is independent of the library's arithmetic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from app.modules.edgar import ratios as r
from app.modules.edgar.xbrl import Fact, FactSet

D = Decimal


def approx(value: Decimal | None, expected: float, tol: float = 0.01) -> None:
    assert value is not None
    assert abs(float(value) - expected) <= tol, (float(value), expected)


def test_aapl_fy2025_matches_the_10k(factset: Callable[[str], FactSet]) -> None:
    fs = factset("AAPL")
    rev = r.revenue(fs)
    assert rev.value == D(416_161_000_000) and rev.period_end == date(2025, 9, 27)
    assert rev.source == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert rev.accession_nos == ("0000320193-25-000079",)
    approx(r.revenue_growth_pct(fs).value, (416_161 / 391_035 - 1) * 100)
    approx(r.operating_margin_pct(fs).value, 133_050 / 416_161 * 100)
    assert r.free_cash_flow(fs, sector="Technology").value == D(111_482_000_000 - 12_715_000_000)
    assert r.eps_diluted(fs).value == D("7.46")
    de = r.debt_to_equity(fs, sector="Technology")
    assert de.value is not None and D("0.5") < de.value < D(3)


def test_nvda_fy2026_revenue_and_growth(factset: Callable[[str], FactSet]) -> None:
    fs = factset("NVDA")
    assert r.revenue(fs).value == D(215_938_000_000)
    assert r.revenue(fs).source == "us-gaap:Revenues"
    approx(r.revenue_growth_pct(fs).value, (215_938 / 130_497 - 1) * 100)
    approx(r.operating_margin_pct(fs).value, 60.38, 0.05)


def test_jnj_uses_pretax_fallback_and_says_so(factset: Callable[[str], FactSet]) -> None:
    fs = factset("JNJ")
    assert r.revenue(fs).value == D(94_193_000_000)
    approx(r.revenue_growth_pct(fs).value, (94_193 / 88_821 - 1) * 100)
    om = r.operating_margin_pct(fs)
    assert om.value is not None
    assert om.note is not None and "Pre-tax" in om.note
    assert om.source == "code: IncomeLossBeforeIncomeTaxes / Revenues"


def test_price_dependent_ratios(factset: Callable[[str], FactSet]) -> None:
    fs = factset("AAPL")
    pe = r.pe_ratio(fs, price=D("268.70"))
    approx(pe.value, 268.70 / 7.46, 0.001)
    assert pe.price == D("268.70")
    dy = r.dividend_yield_pct(fs, price=D("268.70"))
    assert dy.value is not None and D("0.2") < dy.value < D("0.8")
    assert r.pe_ratio(fs, price=None).value is None
    assert r.dividend_yield_pct(fs, price=None).value is None


def test_banks_have_no_fcf_or_debt_to_equity() -> None:
    fs = FactSet([])
    for m in (r.free_cash_flow(fs, sector="Financials"), r.debt_to_equity(fs, sector="Financials")):
        assert m.value is None and m.note == "Not meaningful for banks"


def test_missing_data_returns_none_not_exceptions() -> None:
    fs = FactSet([])
    metrics = r.compute_core_metrics(fs, sector="Technology", price=D(100))
    assert set(metrics) == {
        "revenue",
        "revenue_growth_pct",
        "operating_margin_pct",
        "fcf",
        "debt_to_equity",
        "eps_diluted",
        "pe",
        "dividend_yield_pct",
    }
    assert all(m.value is None for m in metrics.values())


def test_as_of_guard_ignores_facts_filed_later(factset: Callable[[str], FactSet]) -> None:
    fs = factset("AAPL")
    # the FY2025 10-K was filed 2025-10-31; as of Oct 1 2025 the latest is FY2024
    older = r.revenue(fs, as_of=date(2025, 10, 1))
    assert older.period_end == date(2024, 9, 28)
    assert older.value == D(391_035_000_000)
    assert r.revenue(fs, as_of=date(2010, 1, 1)).value is None


def test_restated_value_uses_latest_filing_before_as_of() -> None:
    def fact(val: int, filed: date, accn: str) -> Fact:
        return Fact(
            "us-gaap:Revenues", "USD", date(2024, 1, 1), date(2024, 12, 31), D(val), 2024, "FY", accn, "10-K",
            __import__("datetime").datetime(filed.year, filed.month, filed.day, tzinfo=__import__("datetime").UTC),
        )  # fmt: skip

    fs = FactSet([fact(100, date(2025, 2, 1), "a-1"), fact(110, date(2025, 8, 1), "a-2")])
    assert r.revenue(fs).value == D(110)
    assert r.revenue(fs, as_of=date(2025, 3, 1)).value == D(100)


def test_dividend_yield_zero_for_non_payers(factset: Callable[[str], FactSet]) -> None:
    # synthetic: revenue exists, no dividend facts
    fs = FactSet([f for f in factset("NVDA")._facts if "Dividends" not in f.concept])
    dy = r.dividend_yield_pct(fs, price=D(100))
    assert dy.value == 0 and dy.note is not None


@pytest.mark.parametrize(
    ("value", "population", "expected"),
    [
        (D(5), [D(1), D(2), D(3), D(4), D(5)], D(90)),
        (D(1), [D(1), D(2), D(3), D(4), D(5)], D(10)),
        (D(3), [D(3), D(3)], D(50)),
        (None, [D(1)], None),
        (D(1), [], None),
        (D(1), [None, None], None),
    ],
)
def test_percentile_rank(
    value: Decimal | None, population: list[Decimal | None], expected: Decimal | None
) -> None:
    assert r.percentile_rank(value, population) == expected


def test_percentile_is_bounded_and_monotonic() -> None:
    pop = [D(x) for x in (3, 9, 1, 7, 5, 11, 2)]
    ranks = [r.percentile_rank(x, pop) for x in sorted(pop)]
    assert all(rk is not None and 0 <= rk <= 100 for rk in ranks)
    assert ranks == sorted(ranks)  # type: ignore[type-var]


def test_ordinal() -> None:
    assert [r.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 92)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "92nd",
    ]  # fmt: skip
