"""EDGAR submissions API -> filing references (latest 10-K, recent 8-Ks)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.modules.edgar.client import archive_url


@dataclass(frozen=True, slots=True)
class FilingRef:
    cik: str
    accession_no: str
    form: str
    filed_at: date
    report_date: date | None
    primary_document: str
    items: str = ""

    @property
    def url(self) -> str:
        return archive_url(self.cik, self.accession_no, self.primary_document)

    @property
    def title(self) -> str:
        if self.form.startswith("8-K") and self.items:
            return f"{self.form} (Items {self.items})"
        return f"{self.form} {self.report_date.year if self.report_date else self.filed_at.year}"


def _d(raw: object) -> date | None:
    return date.fromisoformat(str(raw)) if raw else None


def parse_submissions(payload: dict[str, Any], cik: str) -> list[FilingRef]:
    recent = payload.get("filings", {}).get("recent", {})
    cols = {
        k: recent.get(k, [])
        for k in ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument", "items")
    }
    n = len(cols["accessionNumber"])
    out: list[FilingRef] = []
    for i in range(n):
        try:
            filed = _d(cols["filingDate"][i])
            doc = cols["primaryDocument"][i]
            if filed is None or not doc:
                continue
            out.append(
                FilingRef(
                    cik=str(int(cik)),
                    accession_no=cols["accessionNumber"][i],
                    form=cols["form"][i],
                    filed_at=filed,
                    report_date=_d(cols["reportDate"][i] if i < len(cols["reportDate"]) else None),
                    primary_document=doc,
                    items=cols["items"][i] if i < len(cols["items"]) else "",
                )
            )
        except (IndexError, ValueError, TypeError):
            continue
    return out


def select_filings(
    refs: list[FilingRef], *, n_10k: int = 1, n_8k: int = 3, as_of: date | None = None
) -> list[FilingRef]:
    """Newest `n_10k` annual reports and `n_8k` current reports filed on/before `as_of`."""
    pool = [r for r in refs if as_of is None or r.filed_at <= as_of]
    pool.sort(key=lambda r: r.filed_at, reverse=True)
    tenk = [r for r in pool if r.form == "10-K"][:n_10k]
    eightk = [r for r in pool if r.form == "8-K"][:n_8k]
    return tenk + eightk
