"""Alpaca Market Data (Basic plan): REST snapshots/bars + the IEX WebSocket stream.

Endpoints, auth and message shapes follow the Alpaca docs read on 2026-11-01 (see docs/VERIFIED.md, "Stream C"):
* REST  `https://data.alpaca.markets`, `GET /v2/stocks/snapshots?symbols=&feed=`, `GET /v2/stocks/bars`;
  auth headers `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`.
* WS    `wss://stream.data.alpaca.markets/v2/iex`; JSON arrays; `{"action":"auth","key","secret"}` then
  `{"action":"subscribe","trades":[...],"quotes":[...]}`; control messages `T = success | error | subscription`.
**Nothing here has run against the live service** (no keys); behaviour is covered by tests with a fake
WebSocket and respx-mocked REST only. Keys are never logged or placed in exceptions.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from app.core.clock import Clock
from app.core.logging import get_logger
from app.modules.marketdata.sources.base import UpstreamError
from app.modules.marketdata.store import StoredQuote

log = get_logger("app.marketdata.alpaca")

REST_BASE = "https://data.alpaca.markets"
WS_URL_IEX = "wss://stream.data.alpaca.markets/v2/iex"
WS_SOURCE = "alpaca_iex_ws"
REST_SOURCE = "alpaca_rest_delayed"
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
_FRACTION_RE = re.compile(r"\.(\d{1,9})")


class WsConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


WsConnector = Callable[[str], Awaitable[WsConnection]]


async def default_ws_connector(url: str) -> WsConnection:
    from websockets.asyncio.client import connect  # imported lazily: only needed with real keys

    try:
        conn = await connect(url, open_timeout=10, ping_interval=20, ping_timeout=20, max_size=2**20)
    except (OSError, TimeoutError) as exc:
        raise UpstreamError(f"alpaca ws connect failed: {type(exc).__name__}") from None
    return conn


def parse_rfc3339(value: object, fallback: datetime) -> datetime:
    """RFC-3339 with up to nanosecond precision -> aware UTC datetime (extra digits truncated)."""
    if not isinstance(value, str):
        return fallback
    text = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 and d.is_finite() else None


def _safe_symbols(symbols: Sequence[str]) -> list[str]:
    return [s for s in symbols if _SYMBOL_RE.match(s)]


class AlpacaRest:
    """REST fallback (`alpaca_rest_delayed`). 200 calls/min on Basic: one batched call per poll."""

    name = "alpaca_rest"

    def __init__(
        self,
        key_id: str,
        secret: str,
        clock: Clock,
        *,
        http: httpx.AsyncClient | None = None,
        feed: str = "iex",
        base_url: str = REST_BASE,
    ) -> None:
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self._clock = clock
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        self._owns_http = http is None
        self._feed = feed
        self._base = base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        try:
            resp = await self._http.get(f"{self._base}{path}", params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"alpaca rest transport error: {type(exc).__name__}") from None
        if resp.status_code in (401, 403):
            raise UpstreamError(f"alpaca rest auth failed ({resp.status_code})")
        if resp.status_code == 429:
            raise UpstreamError("alpaca rest rate limited (429)")
        if resp.status_code >= 400:
            raise UpstreamError(f"alpaca rest http {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise UpstreamError("alpaca rest returned non-JSON") from None
        if not isinstance(body, dict):
            raise UpstreamError("alpaca rest unexpected payload")
        return body

    async def fetch(self, symbols: Sequence[str]) -> dict[str, StoredQuote]:
        syms = _safe_symbols(symbols)
        if not syms:
            return {}
        body = await self._get("/v2/stocks/snapshots", {"symbols": ",".join(syms), "feed": self._feed})
        now = self._clock.now()
        out: dict[str, StoredQuote] = {}
        for symbol, snap in body.items():
            if symbol not in syms or not isinstance(snap, dict):
                continue
            quote = self._snapshot_to_quote(symbol, snap, now)
            if quote is not None:
                out[symbol] = quote
        return out

    @staticmethod
    def _snapshot_to_quote(symbol: str, snap: dict[str, Any], now: datetime) -> StoredQuote | None:
        trade = snap.get("latestTrade") or {}
        q = snap.get("latestQuote") or {}
        bid, ask = _dec(q.get("bp")), _dec(q.get("ap"))
        price = _dec(trade.get("p"))
        ts = parse_rfc3339(trade.get("t"), now)
        if price is None and bid is not None and ask is not None:
            price, ts = (bid + ask) / 2, parse_rfc3339(q.get("t"), now)
        if price is None:
            return None
        prev = snap.get("prevDailyBar") or {}
        return StoredQuote(
            symbol=symbol,
            price=price,
            ts=ts,
            received_at=now,
            source=REST_SOURCE,
            kind="polled",
            bid=bid,
            ask=ask,
            prev_close=_dec(prev.get("c")),
        )

    async def fetch_daily_bars(
        self, symbols: Sequence[str], start: str, end: str | None = None, adjustment: str = "split"
    ) -> dict[str, list[dict[str, Any]]]:
        """Daily candles (`timeframe=1Day`) following `next_page_token`; keys are symbols."""
        syms = _safe_symbols(symbols)
        result: dict[str, list[dict[str, Any]]] = {s: [] for s in syms}
        if not syms:
            return result
        params = {
            "symbols": ",".join(syms),
            "timeframe": "1Day",
            "start": start,
            "adjustment": adjustment,
            "feed": self._feed,
            "limit": "10000",
        }
        if end:
            params["end"] = end
        for _ in range(50):  # hard page cap
            body = await self._get("/v2/stocks/bars", params)
            for symbol, bars in (body.get("bars") or {}).items():
                if symbol in result and isinstance(bars, list):
                    result[symbol].extend(b for b in bars if isinstance(b, dict))
            token = body.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": str(token)}
        return result


class AlpacaStream:
    """IEX WebSocket. `stream()` yields quotes until the socket drops, then raises `UpstreamError`."""

    name = "alpaca_ws"

    def __init__(
        self,
        key_id: str,
        secret: str,
        clock: Clock,
        *,
        connector: WsConnector = default_ws_connector,
        url: str = WS_URL_IEX,
    ) -> None:
        self._key = key_id
        self._secret = secret
        self._clock = clock
        self._connector = connector
        self._url = url

    async def _recv_messages(self, conn: WsConnection) -> list[dict[str, Any]]:
        raw = await conn.recv()
        try:
            data = json.loads(raw)
        except ValueError:
            raise UpstreamError("alpaca ws sent non-JSON") from None
        msgs = data if isinstance(data, list) else [data]
        return [m for m in msgs if isinstance(m, dict)]

    @staticmethod
    def _raise_if_error(msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            if m.get("T") == "error":
                raise UpstreamError(f"alpaca ws error {m.get('code')}: {str(m.get('msg'))[:80]}")

    async def stream(self, symbols: Sequence[str]) -> AsyncIterator[StoredQuote]:
        syms = _safe_symbols(symbols)
        if not syms:
            return
        conn = await self._connector(self._url)
        try:
            first = await self._recv_messages(conn)
            self._raise_if_error(first)
            await conn.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
            authed = False
            while not authed:
                msgs = await self._recv_messages(conn)
                self._raise_if_error(msgs)
                authed = any(m.get("T") == "success" and m.get("msg") == "authenticated" for m in msgs)
            await conn.send(json.dumps({"action": "subscribe", "trades": syms, "quotes": syms}))
            last_price: dict[str, Decimal] = {}
            last_book: dict[str, tuple[Decimal | None, Decimal | None]] = {}
            while True:
                msgs = await self._recv_messages(conn)
                self._raise_if_error(msgs)
                now = self._clock.now()
                for m in msgs:
                    kind, symbol = m.get("T"), m.get("S")
                    if kind not in ("t", "q") or not isinstance(symbol, str):
                        continue
                    if kind == "t":
                        price = _dec(m.get("p"))
                        if price is None:
                            continue
                        last_price[symbol] = price
                        ts = parse_rfc3339(m.get("t"), now)
                    else:
                        bid, ask = _dec(m.get("bp")), _dec(m.get("ap"))
                        last_book[symbol] = (bid, ask)
                        ts = parse_rfc3339(m.get("t"), now)
                        if symbol not in last_price and bid is not None and ask is not None:
                            last_price[symbol] = (bid + ask) / 2
                        if symbol not in last_price:
                            continue
                    bid, ask = last_book.get(symbol, (None, None))
                    yield StoredQuote(
                        symbol=symbol,
                        price=last_price[symbol],
                        ts=ts,
                        received_at=now,
                        source=WS_SOURCE,
                        kind="stream",
                        bid=bid,
                        ask=ask,
                    )
        finally:
            try:
                await conn.close()
            except Exception as exc:
                log.debug("ws close failed", extra={"error": type(exc).__name__})
