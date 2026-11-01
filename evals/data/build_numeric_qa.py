"""Builds evals/data/numeric_qa.jsonl: 30 numeric questions (5 companies x 6 facts) with answers read DIRECTLY
from the recorded XBRL companyfacts fixtures (no ratio-library code involved), plus the accession number the
answer must cite. Run: uv run python evals/data/build_numeric_qa.py"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "edgar"
COMPANIES = {
    "AAPL": "Apple Inc.",
    "JNJ": "Johnson & Johnson",
    "NVDA": "NVIDIA Corp.",
    "JPM": "JPMorgan Chase",
    "KO": "Coca-Cola Co.",
}
# (key, question text, concept candidates in priority order, unit, kind)
FACTS = [
    (
        "revenue",
        "revenue",
        [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ],
        "USD",
        "annual",
    ),
    ("net_income", "net income", ["NetIncomeLoss"], "USD", "annual"),
    (
        "operating_cash_flow",
        "net cash provided by operating activities",
        ["NetCashProvidedByUsedInOperatingActivities"],
        "USD",
        "annual",
    ),
    ("eps_diluted", "diluted earnings per share", ["EarningsPerShareDiluted"], "USD/shares", "annual"),
    (
        "equity",
        "total stockholders' equity",
        ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
        "USD",
        "instant",
    ),
    (
        "operating_or_pretax_income",
        "operating income (pre-tax income for banks)",
        [
            "OperatingIncomeLoss",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        ],
        "USD",
        "annual",
    ),
]


def latest(payload: dict, candidates: list[str], unit: str, kind: str) -> dict | None:
    best: dict | None = None
    for name in candidates:
        entries = payload["facts"].get("us-gaap", {}).get(name, {}).get("units", {}).get(unit, [])
        for e in entries:
            if e.get("form") != "10-K":
                continue
            if kind == "annual":
                if (
                    "start" not in e
                    or not 350 <= (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days <= 380
                ):
                    continue
            elif "start" in e:
                continue
            key = (e["end"], e["filed"])
            if best is None or key > (best["end"], best["filed"]):
                best = e
        if best:
            return best  # first concept (priority order) that has data wins
    return best


def main() -> None:
    rows = []
    for sym, name in COMPANIES.items():
        payload = json.loads((FIX / f"companyfacts_{sym}.json").read_text())
        for key, text, concepts, unit, kind in FACTS:
            e = latest(payload, concepts, unit, kind)
            assert e is not None, (sym, key)
            rows.append(
                {
                    "id": f"nqa-{len(rows) + 1:02d}",
                    "symbol": sym,
                    "metric": key,
                    "question": f"What was {name}'s {text} for its latest fiscal year (period ending {e['end']})?",
                    "answer": e["val"],
                    "unit": unit,
                    "period_end": e["end"],
                    "accession_no": e["accn"],
                    "form": e["form"],
                }
            )
    (Path(__file__).parent / "numeric_qa.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(len(rows), "questions")


if __name__ == "__main__":
    main()
