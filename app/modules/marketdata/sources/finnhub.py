"""Finnhub REST quote fallback (`GET https://finnhub.io/api/v1/quote?symbol=`, auth header `X-Finnhub-Token`).

Endpoint, base URL, auth options and the `c/pc/d/dp/o/h/l` fields were read from Finnhub's published Swagger
(docs/VERIFIED.md). The `t` (unix seconds) field and the 60 calls/min free-plan limit are NOT in that spec and
are treated as unverified: a missing `t` falls back to receive time, and `fetch` paces itself well below 60/min.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.clock import Clock
from app.modules.marketdata.sources.base import UpstreamError
from app.modules.marketdata.store import StoredQuote

BASE_URL = "https://finnhub.io/api/v1"
SOURCE = "finnhub"
CHUNK = 3  # one HTTP call per symbol: the supervisor polls CHUNK symbols per interval (5 s) = 36 calls/min


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 and d.is_finite() else None


class FinnhubRest:
    name = "finnhub"
    chunk = CHUNK

    def __init__(self, token: str, clock: Clock, *, http: httpx.AsyncClient | None = None) -> None:
        self._headers = {"X-Finnhub-Token": token}
        self._clock = clock
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def quote(self, symbol: str) -> StoredQuote | None:
        try:
            resp = await self._http.get(f"{BASE_URL}/quote", params={"symbol": symbol}, headers=self._headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"finnhub transport error: {type(exc).__name__}") from None
        if resp.status_code in (401, 403):
            raise UpstreamError(f"finnhub auth failed ({resp.status_code})")
        if resp.status_code == 429:
            raise UpstreamError("finnhub rate limited (429)")
        if resp.status_code >= 400:
            raise UpstreamError(f"finnhub http {resp.status_code}")
        try:
            body: Any = resp.json()
        except ValueError:
            raise UpstreamError("finnhub returned non-JSON") from None
        if not isinstance(body, dict):
            raise UpstreamError("finnhub unexpected payload")
        price = _dec(body.get("c"))  # Finnhub returns c = 0 for unknown symbols
        if price is None:
            return None
        now = self._clock.now()
        t = body.get("t")
        ts = datetime.fromtimestamp(float(t), UTC) if isinstance(t, int | float) and t > 0 else now
        return StoredQuote(
            symbol=symbol,
            price=price,
            ts=min(ts, now),
            received_at=now,
            source=SOURCE,
            kind="polled",
            prev_close=_dec(body.get("pc")),
        )

    async def fetch(self, symbols: Sequence[str]) -> dict[str, StoredQuote]:
        out: dict[str, StoredQuote] = {}
        for symbol in symbols:
            q = await self.quote(symbol)
            if q is not None:
                out[symbol] = q
        return out
