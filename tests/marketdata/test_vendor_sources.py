"""Alpaca (REST via respx, WS via a fake socket) and Finnhub (respx). No network: payloads follow the docs
recorded in docs/VERIFIED.md, so these tests prove *our* parsing, not the vendors' behaviour."""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from app.core.clock import FakeClock
from app.modules.marketdata.sources.alpaca import (
    REST_BASE,
    AlpacaRest,
    AlpacaStream,
    parse_rfc3339,
)
from app.modules.marketdata.sources.base import UpstreamError
from app.modules.marketdata.sources.finnhub import BASE_URL, FinnhubRest

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
KEY, SECRET = "PKTESTKEY123", "SECRETVALUE456"


def test_rfc3339_nanoseconds_and_fallback() -> None:
    assert parse_rfc3339("2026-10-30T14:59:58.123456789Z", NOW) == datetime(
        2026, 10, 30, 14, 59, 58, 123456, tzinfo=UTC
    )
    assert parse_rfc3339("2026-10-30T14:59:58Z", NOW).second == 58
    assert parse_rfc3339("garbage", NOW) == NOW and parse_rfc3339(None, NOW) == NOW


# ------------------------------------------------------------------------------------------ REST
SNAPSHOTS = {
    "AAPL": {
        "latestTrade": {"t": "2026-10-30T14:45:01.5Z", "p": 268.71, "s": 100, "x": "V"},
        "latestQuote": {"t": "2026-10-30T14:45:02Z", "bp": 268.7, "ap": 268.72, "bs": 1, "as": 2},
        "prevDailyBar": {"t": "2026-10-29T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 267.9, "v": 10},
    },
    "MSFT": {"latestQuote": {"t": "2026-10-30T14:45:02Z", "bp": 520.0, "ap": 521.0}},
    "NODATA": {},
}


@respx.mock
async def test_alpaca_snapshots_parse_and_send_auth_headers() -> None:
    route = respx.get(f"{REST_BASE}/v2/stocks/snapshots").mock(
        return_value=httpx.Response(200, json=SNAPSHOTS)
    )
    rest = AlpacaRest(KEY, SECRET, FakeClock(NOW))
    got = await rest.fetch(["AAPL", "MSFT", "NODATA", "bad symbol!"])
    req = route.calls[0].request
    assert req.headers["APCA-API-KEY-ID"] == KEY and req.headers["APCA-API-SECRET-KEY"] == SECRET
    assert dict(req.url.params) == {"symbols": "AAPL,MSFT,NODATA", "feed": "iex"}  # invalid symbol never sent
    a = got["AAPL"]
    assert (a.price, a.bid, a.ask, a.prev_close) == (
        Decimal("268.71"),
        Decimal("268.7"),
        Decimal("268.72"),
        Decimal("267.9"),
    )
    assert a.source == "alpaca_rest_delayed" and a.kind == "polled" and a.received_at == NOW
    assert got["MSFT"].price == Decimal("520.5")  # no trade -> mid of bid/ask
    assert "NODATA" not in got
    await rest.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "auth failed"), (403, "auth failed"), (429, "rate limited"), (500, "http 500")],
)
async def test_alpaca_rest_errors_become_upstream_error_without_leaking_keys(
    status: int, fragment: str
) -> None:
    respx.get(f"{REST_BASE}/v2/stocks/snapshots").mock(
        return_value=httpx.Response(status, text=f"{KEY} {SECRET}")
    )
    rest = AlpacaRest(KEY, SECRET, FakeClock(NOW))
    with pytest.raises(UpstreamError, match=fragment) as exc:
        await rest.fetch(["AAPL"])
    assert KEY not in str(exc.value) and SECRET not in str(exc.value)


@respx.mock
async def test_alpaca_rest_transport_and_payload_errors() -> None:
    rest = AlpacaRest(KEY, SECRET, FakeClock(NOW))
    respx.get(f"{REST_BASE}/v2/stocks/snapshots").mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(UpstreamError, match="transport"):
        await rest.fetch(["AAPL"])
    respx.get(f"{REST_BASE}/v2/stocks/snapshots").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(UpstreamError, match="non-JSON"):
        await rest.fetch(["AAPL"])
    respx.get(f"{REST_BASE}/v2/stocks/snapshots").mock(return_value=httpx.Response(200, json=[1]))
    with pytest.raises(UpstreamError, match="unexpected"):
        await rest.fetch(["AAPL"])
    assert await rest.fetch([]) == {}


@respx.mock
async def test_alpaca_daily_bars_follow_pagination() -> None:
    page1 = {
        "bars": {"AAPL": [{"t": "2026-10-28T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 5}]},
        "next_page_token": "tok",
    }
    page2 = {
        "bars": {"AAPL": [{"t": "2026-10-29T04:00:00Z", "o": 2, "h": 3, "l": 2, "c": 3, "v": 6}]},
        "next_page_token": None,
    }
    route = respx.get(f"{REST_BASE}/v2/stocks/bars").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    rest = AlpacaRest(KEY, SECRET, FakeClock(NOW))
    out = await rest.fetch_daily_bars(["AAPL"], "2026-10-01")
    assert [b["c"] for b in out["AAPL"]] == [2, 3]
    first, second = (c.request.url.params for c in route.calls)
    assert first["timeframe"] == "1Day" and first["adjustment"] == "split" and "page_token" not in first
    assert second["page_token"] == "tok"


# ------------------------------------------------------------------------------------------ WS
class FakeWs:
    def __init__(self, incoming: list[object]) -> None:
        self.incoming: deque[object] = deque(incoming)
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str | bytes:
        if not self.incoming:
            raise UpstreamError("connection closed")
        item = self.incoming.popleft()
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str | bytes) else json.dumps(item)

    async def close(self) -> None:
        self.closed = True


HANDSHAKE = [
    [{"T": "success", "msg": "connected"}],
    [{"T": "success", "msg": "authenticated"}],
    [{"T": "subscription", "trades": ["AAPL"], "quotes": ["AAPL"]}],
]


def stream_with(ws: FakeWs) -> AlpacaStream:
    async def connector(url: str) -> FakeWs:
        assert url == "wss://stream.data.alpaca.markets/v2/iex"
        return ws

    return AlpacaStream(KEY, SECRET, FakeClock(NOW), connector=connector)


async def test_ws_handshake_subscribe_and_quote_assembly() -> None:
    ws = FakeWs(
        [
            *HANDSHAKE[:2],
            [
                {
                    "T": "q",
                    "S": "AAPL",
                    "bp": 268.69,
                    "ap": 268.73,
                    "t": "2026-10-30T14:59:59.9Z",
                },  # quote first
                {"T": "t", "S": "AAPL", "p": 268.71, "s": 10, "t": "2026-10-30T14:59:59.95Z"},
            ],
            [
                {"T": "t", "S": "AAPL", "p": 268.8, "s": 1, "t": "2026-10-30T15:00:00Z"},
                {"T": "b", "S": "AAPL"},
            ],
        ]
    )
    got = []
    with pytest.raises(UpstreamError):  # the fake closes after the script
        async for q in stream_with(ws).stream(["AAPL", "bad symbol"]):
            got.append(q)
    assert ws.sent[0] == {"action": "auth", "key": KEY, "secret": SECRET}
    assert ws.sent[1] == {"action": "subscribe", "trades": ["AAPL"], "quotes": ["AAPL"]}
    assert [q.price for q in got] == [Decimal("268.71"), Decimal("268.71"), Decimal("268.8")]
    assert all(q.source == "alpaca_iex_ws" and q.kind == "stream" for q in got)
    assert got[-1].bid == Decimal("268.69") and got[-1].ask == Decimal("268.73")  # book carried onto trades
    assert ws.closed


async def test_ws_quote_before_any_trade_uses_midpoint() -> None:
    ws = FakeWs([*HANDSHAKE[:2], [{"T": "q", "S": "AAPL", "bp": 10, "ap": 12, "t": "2026-10-30T15:00:00Z"}]])
    async for q in stream_with(ws).stream(["AAPL"]):
        assert q.price == Decimal("11")
        break


async def test_ws_auth_failure_and_connection_limit_raise_without_leaking_secret() -> None:
    ws = FakeWs(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "error", "code": 402, "msg": f"auth failed {SECRET}"}],
        ]
    )
    with pytest.raises(UpstreamError, match="402") as exc:
        async for _ in stream_with(ws).stream(["AAPL"]):
            pass
    assert ws.closed
    assert KEY not in str(exc.value)  # message is truncated to the server msg; key id never added
    ws2 = FakeWs([[{"T": "error", "code": 406, "msg": "connection limit exceeded"}]])
    with pytest.raises(UpstreamError, match="406"):
        async for _ in stream_with(ws2).stream(["AAPL"]):
            pass


async def test_ws_non_json_and_no_symbols() -> None:
    ws = FakeWs([[{"T": "success", "msg": "connected"}], b"\xff\xfe not json"])
    with pytest.raises(UpstreamError, match="non-JSON"):
        async for _ in stream_with(ws).stream(["AAPL"]):
            pass
    ws2 = FakeWs([])
    assert [q async for q in stream_with(ws2).stream([])] == []


# ------------------------------------------------------------------------------------- Finnhub
@respx.mock
async def test_finnhub_quote_parsing_and_header_auth() -> None:
    route = respx.get(f"{BASE_URL}/quote").mock(
        return_value=httpx.Response(
            200,
            json={
                "c": 268.7,
                "d": 0.8,
                "dp": 0.3,
                "h": 270,
                "l": 267,
                "o": 268,
                "pc": 267.9,
                "t": 1793372400,
            },
        )
    )
    fh = FinnhubRest("tok123", FakeClock(NOW))
    got = await fh.fetch(["AAPL"])
    req = route.calls[0].request
    assert req.headers["X-Finnhub-Token"] == "tok123" and "tok123" not in str(
        req.url
    )  # not in the query string
    q = got["AAPL"]
    assert (q.price, q.prev_close, q.source, q.kind) == (
        Decimal("268.7"),
        Decimal("267.9"),
        "finnhub",
        "polled",
    )
    assert q.ts <= NOW


@respx.mock
async def test_finnhub_zero_price_missing_t_and_errors() -> None:
    fh = FinnhubRest("tok", FakeClock(NOW))
    respx.get(f"{BASE_URL}/quote").mock(return_value=httpx.Response(200, json={"c": 0, "pc": 0}))
    assert await fh.fetch(["ZZZ"]) == {}  # unknown symbols come back as zeros
    respx.get(f"{BASE_URL}/quote").mock(return_value=httpx.Response(200, json={"c": 10, "pc": 9}))
    q = (await fh.fetch(["AAPL"]))["AAPL"]
    assert q.ts == NOW  # no `t` -> receive time
    for status, frag in [(401, "auth"), (429, "rate limited"), (503, "http 503")]:
        respx.get(f"{BASE_URL}/quote").mock(return_value=httpx.Response(status))
        with pytest.raises(UpstreamError, match=frag):
            await fh.fetch(["AAPL"])
    respx.get(f"{BASE_URL}/quote").mock(side_effect=httpx.ReadTimeout("x"))
    with pytest.raises(UpstreamError, match="transport"):
        await fh.fetch(["AAPL"])
    respx.get(f"{BASE_URL}/quote").mock(return_value=httpx.Response(200, text="nope"))
    with pytest.raises(UpstreamError, match="non-JSON"):
        await fh.fetch(["AAPL"])
