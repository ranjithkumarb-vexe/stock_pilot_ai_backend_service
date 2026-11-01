"""Next earnings date per ticker. No paid calendar is wired in this build, so the default source ESTIMATES the
next report from the dates of the company's previous "results" 8-Ks (Item 2.02) and labels the alert as
estimated; a vendor calendar can replace it by implementing `EarningsSource`."""

from __future__ import annotations

import itertools
import statistics
from datetime import date, timedelta
from typing import Protocol

from app.core.db import Connection

QUARTER = 91
MIN_GAP, MAX_GAP = 60, 130


class EarningsSource(Protocol):
    async def next_report(self, conn: Connection, symbol: str, today: date) -> tuple[date, bool] | None:
        """`(report_date, estimated)` or None when unknown."""
        ...


def estimate_next(dates: list[date], today: date) -> date | None:
    """Last results date + the typical gap (median of plausible quarterly gaps, default 91 days)."""
    ds = sorted(set(dates))
    if not ds:
        return None
    gaps = [(b - a).days for a, b in itertools.pairwise(ds) if MIN_GAP <= (b - a).days <= MAX_GAP]
    gap = round(statistics.median(gaps)) if gaps else QUARTER
    nxt = ds[-1] + timedelta(days=gap)
    for _ in range(8):
        if nxt >= today:
            return nxt
        nxt += timedelta(days=gap)
    return None


class FilingHistoryEarnings:
    async def next_report(self, conn: Connection, symbol: str, today: date) -> tuple[date, bool] | None:
        rows = await conn.fetch(
            "select filed_at::date as d from raw_documents where symbol = $1 and form like '8-K%' "
            "and title ~ '2\\.02' order by filed_at desc limit 6",
            symbol,
        )
        nxt = estimate_next([r["d"] for r in rows], today)
        return None if nxt is None else (nxt, True)
