"""Where new 8-Ks come from (BE-17). Every source returns `FilingSignal`s; the detector merges the same
accession seen by several sources into ONE alert (T-ALERT-01).

* `RawDocumentsSource`  - 8-Ks the filing ingest (BE-12) already stored in `raw_documents`;
* `EdgarSubmissionsSource` - polls `data.sec.gov/submissions/CIK##########.json` through the hardened
  `EdgarClient` (<= 8 req/s, declared User-Agent, backoff on 429/403). **Unverified against the live service
  in this build** (see docs/VERIFIED.md); tests use recorded payloads and a fake transport.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any, Protocol

from app.core.db import Connection, get_db
from app.core.logging import get_logger
from app.modules.alerts.detectors import FilingSignal
from app.modules.alerts.materiality import split_items
from app.modules.edgar.client import EdgarClient, EdgarError, archive_url, submissions_url

log = get_logger("app.alerts.filings")

_ITEMS_RE = re.compile(r"Items?\s+([0-9.,\s]+)")


class FilingSource(Protocol):
    name: str

    async def poll(self, symbols: Sequence[str], since: datetime) -> list[FilingSignal]: ...


def items_from_title(title: str | None) -> tuple[str, ...]:
    """`"8-K (Items 2.02,9.01)"` -> `("2.02", "9.01")`."""
    m = _ITEMS_RE.search(title or "")
    return tuple(split_items(m.group(1))) if m else ()


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=UTC)


def parse_8k_signals(
    payload: Mapping[str, Any], symbol: str, cik: str, source: str = "sec_edgar"
) -> list[FilingSignal]:
    """Signals for the 8-Ks in an EDGAR submissions payload (`acceptanceDateTime` gives minute-level timing)."""
    recent = payload.get("filings", {}).get("recent", {})
    cols = {
        k: recent.get(k, [])
        for k in ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "items", "primaryDocument")
    }
    out: list[FilingSignal] = []
    for i, acc in enumerate(cols["accessionNumber"]):
        try:
            if cols["form"][i] != "8-K":
                continue
            accepted = cols["acceptanceDateTime"][i] if i < len(cols["acceptanceDateTime"]) else None
            if accepted:
                published = datetime.fromisoformat(str(accepted).replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
            else:
                published = _midnight(date.fromisoformat(cols["filingDate"][i]))
            doc = cols["primaryDocument"][i] if i < len(cols["primaryDocument"]) else ""
            raw_items = cols["items"][i] if i < len(cols["items"]) else ""
            out.append(
                FilingSignal(
                    symbol=symbol,
                    accession_no=acc,
                    items=tuple(split_items(raw_items)),
                    published_at=published.astimezone(UTC),
                    source=source,
                    url=archive_url(cik, acc, doc) if doc else None,
                )
            )
        except (IndexError, ValueError, TypeError):
            continue
    return out


class RawDocumentsSource:
    """8-Ks already ingested into `raw_documents` (reference table, readable by the app role)."""

    name = "raw_documents"

    async def poll(self, symbols: Sequence[str], since: datetime) -> list[FilingSignal]:
        if not symbols:
            return []
        async with get_db().pool.acquire() as conn:
            return await self.poll_conn(conn, symbols, since)

    async def poll_conn(
        self, conn: Connection, symbols: Sequence[str], since: datetime
    ) -> list[FilingSignal]:
        rows = await conn.fetch(
            "select symbol, accession_no, title, url, filed_at from raw_documents "
            "where form like '8-K%' and not quarantined and symbol = any($1::text[]) and filed_at >= $2 "
            "order by filed_at desc",
            list(symbols),
            since,
        )
        return [
            FilingSignal(
                r["symbol"],
                r["accession_no"],
                items_from_title(r["title"]),
                r["filed_at"],
                self.name,
                r["url"],
            )
            for r in rows
        ]


class EdgarSubmissionsSource:
    """Live poll of the SEC submissions feed (one request per ticker per cycle, cached for `max_age_s`)."""

    name = "sec_edgar"

    def __init__(
        self,
        client: EdgarClient,
        ciks: Callable[[], Any] | Mapping[str, str],
        *,
        max_age_s: float = 60.0,
    ) -> None:
        self._client = client
        self._ciks = ciks
        self._max_age_s = max_age_s
        self.errors = 0

    async def _cik_map(self) -> Mapping[str, str]:
        if isinstance(self._ciks, Mapping):
            return self._ciks
        found = await self._ciks()
        return found  # type: ignore[no-any-return]

    async def poll(self, symbols: Sequence[str], since: datetime) -> list[FilingSignal]:
        ciks = await self._cik_map()
        out: list[FilingSignal] = []
        for sym in symbols:
            cik = ciks.get(sym)
            if not cik:
                continue
            try:
                payload = await self._client.get_json(submissions_url(cik), max_age_s=self._max_age_s)
            except EdgarError as exc:  # 404 / retries exhausted: skip this ticker this cycle
                self.errors += 1
                log.warning("submissions poll failed", extra={"symbol": sym, "error": type(exc).__name__})
                continue
            out.extend(s for s in parse_8k_signals(payload, sym, cik) if s.published_at >= since)
        return out


async def load_ciks() -> dict[str, str]:
    async with get_db().pool.acquire() as conn:
        rows = await conn.fetch("select symbol, cik from tickers where cik is not null and in_universe")
    return {r["symbol"]: r["cik"] for r in rows}
