"""Market-data API: quotes, chart bars, market status, SSE stream, dev market/feed controls (BE-09)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.core.auth import consume_stream_token
from app.core.clock import Clock
from app.core.db import Connection, user_tx
from app.core.deps import CurrentUser, SettingsDep, UserConn
from app.core.errors import AppError, ErrorCode, not_found, unauthenticated, validation_error
from app.core.logging import get_logger
from app.modules.marketdata.bars import (
    BARS_SOURCE_1D,
    BARS_SOURCE_5M,
    Bar,
    daily_bars,
    synth_bars_5m,
)
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.factory import build_service
from app.modules.marketdata.hub import StreamEvent
from app.modules.marketdata.schemas import (
    BarOut,
    BarsResponse,
    DevFeedRequest,
    DevFeedResponse,
    DevMarketRequest,
    DevMarketResponse,
    MarketStatus,
    QuoteOut,
    QuotesResponse,
)
from app.modules.marketdata.service import MarketDataService

log = get_logger("app.marketdata.router")

router = APIRouter(tags=["market data"])

_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
MAX_QUOTE_SYMBOLS = 50
MAX_STREAMS_PER_USER = 5


def get_service(request: Request) -> MarketDataService:
    svc: MarketDataService | None = getattr(request.app.state, "market_service", None)
    if svc is None:
        raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, "Market data service not running")
    return svc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = app.state
    if not hasattr(state, "market_override"):
        state.market_override = None
    if not hasattr(state, "feed_override"):
        state.feed_override = None
    cfg: MarketDataConfig = getattr(state, "marketdata_config", None) or MarketDataConfig.from_settings(
        state.settings
    )
    svc = build_service(cfg, state.clock, state)
    state.market_service = svc
    state.price_source = svc.price_source
    state.stream_hub = svc.hub
    await svc.start()
    log.info("market data started", extra={"source": "simulated" if svc.is_simulated else cfg.source})
    try:
        yield
    finally:
        await svc.stop()


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _parse_symbols(raw: str) -> list[str]:
    symbols: list[str] = []
    for part in raw.split(","):
        s = part.strip().upper()
        if not s:
            continue
        if not _SYMBOL_RE.match(s):
            raise validation_error("Invalid symbol", {"symbol": s[:12]})
        if s not in symbols:
            symbols.append(s)
    if not symbols:
        raise validation_error("`symbols` is required")
    if len(symbols) > MAX_QUOTE_SYMBOLS:
        raise validation_error(f"At most {MAX_QUOTE_SYMBOLS} symbols per request")
    return symbols


async def _require_supported(conn: Connection, symbols: list[str]) -> None:
    rows = await conn.fetch(
        "select symbol from tickers where symbol = any($1::text[]) and in_universe", symbols
    )
    known = {r["symbol"] for r in rows}
    missing = [s for s in symbols if s not in known]
    if missing:
        raise AppError(
            ErrorCode.UNSUPPORTED_SYMBOL,
            "Symbol is not in the supported universe",
            details={"symbols": missing},
        )


# ---------------------------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------------------------
@router.get("/quotes", response_model=QuotesResponse, summary="Latest quotes with session + freshness")
async def get_quotes(
    user: CurrentUser,
    conn: UserConn,
    request: Request,
    symbols: Annotated[str, Query(description="comma-separated tickers")],
) -> QuotesResponse:
    del user
    wanted = _parse_symbols(symbols)
    await _require_supported(conn, wanted)
    svc = get_service(request)
    found = await svc.get_quotes(wanted)
    return QuotesResponse(
        items=[QuoteOut.from_quote(found[s]) for s in wanted if s in found],
        unavailable=[s for s in wanted if s not in found],
    )


@router.get(
    "/quotes/{symbol}/bars", response_model=BarsResponse, summary="Chart bars (5m simulated, 1d stored)"
)
async def get_bars(
    symbol: str,
    user: CurrentUser,
    conn: UserConn,
    request: Request,
    interval: Annotated[Literal["5m", "1d"], Query()] = "1d",
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> BarsResponse:
    del user
    sym = symbol.strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise validation_error("Invalid symbol")
    await _require_supported(conn, [sym])
    svc = get_service(request)
    bars: list[Bar]
    if interval == "1d":
        bars = await daily_bars(conn, sym, limit or 120)
        source = BARS_SOURCE_1D
    else:
        sim = svc.simulated
        anchor = float(sim.anchor(sym)) if sim else float(svc.store.prev_close(sym) or 0) or 100.0
        quote = await svc.get_quote(sym)
        bars = synth_bars_5m(
            symbol=sym,
            calendar=svc.calendar,
            now=svc.clock.now(),
            anchor=anchor,
            last_price=float(quote.price) if quote else None,
            seed=svc.cfg.sim_seed,
            sigma=svc.cfg.sim_sigma,
            limit=limit or 78,
        )
        source = BARS_SOURCE_5M
    return BarsResponse(
        symbol=sym,
        interval=interval,
        bars=[
            BarOut(time=b.time, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
            for b in bars
        ],
        source=source,
    )


@router.get("/market/status", response_model=MarketStatus, summary="NYSE session + feed state")
async def market_status(user: CurrentUser, request: Request) -> MarketStatus:
    del user
    svc = get_service(request)
    now = svc.clock.now()
    session = svc.session_now()
    return MarketStatus(
        open=session == "regular",
        session=session,
        as_of=now,
        next_open=svc.calendar.next_open(now),
        next_close=svc.calendar.next_close(now),
        early_close=svc.calendar.is_early_close(now),
        source="simulated" if svc.is_simulated else svc.cfg.source,
        simulated=svc.is_simulated,
        feed_state=svc.feed_state,
        override=svc.market_override,
    )


# ---------------------------------------------------------------------------------------------
# dev-only controls (AUTH_MODE=dev): drive the UI's "simulate" buttons
# ---------------------------------------------------------------------------------------------
@router.post("/dev/market", response_model=DevMarketResponse, summary="Dev only: force market open/closed")
async def dev_market(
    body: DevMarketRequest, user: CurrentUser, request: Request, settings: SettingsDep
) -> DevMarketResponse:
    del user
    if settings.auth_mode != "dev":
        raise not_found()
    svc = get_service(request)
    svc.set_market_override(body.open)
    session = svc.session_now()
    return DevMarketResponse(open=session == "regular", override=svc.market_override, session=session)


@router.post("/dev/feed", response_model=DevFeedResponse, summary="Dev only: force the feed state")
async def dev_feed(
    body: DevFeedRequest, user: CurrentUser, request: Request, settings: SettingsDep
) -> DevFeedResponse:
    del user
    if settings.auth_mode != "dev":
        raise not_found()
    svc = get_service(request)
    svc.set_feed_override(None if body.state == "live" else body.state)
    return DevFeedResponse(state=svc.feed_state)


# ---------------------------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------------------------
async def _user_symbols(user_id: UUID) -> set[str]:
    async with user_tx(user_id) as conn:
        rows = await conn.fetch(
            "select symbol from watchlist_items union select symbol from positions where qty > 0"
        )
    return {r["symbol"] for r in rows}


def _parse_last_id(header: str | None, query: str | None) -> int | None:
    for raw in (header, query):
        if raw and raw.strip().isdigit():
            return int(raw.strip())
    return None


async def _sse_events(
    svc: MarketDataService, user_id: UUID, last_id: int | None, symbols: set[str]
) -> AsyncIterator[bytes]:
    hub = svc.hub
    sub = hub.subscribe(user_id, symbols)
    loop = asyncio.get_running_loop()
    cfg = svc.cfg
    try:
        yield b"retry: 3000\n\n"
        sent_up_to = 0
        if last_id is not None:
            backlog = hub.replay(user_id, symbols, last_id)
        else:  # fresh connection: current quote of every symbol the user cares about
            snap = await svc.get_quotes(sorted(symbols))
            backlog = [
                StreamEvent(hub.next_id(), "quote", QuoteOut.from_quote(q).model_dump(mode="json"), None, s)
                for s, q in snap.items()
            ]
        for ev in backlog:
            sent_up_to = max(sent_up_to, ev.id)
            yield ev.encode()
        next_beat = loop.time() + cfg.heartbeat_s
        next_refresh = loop.time() + cfg.stream_symbols_refresh_s
        while True:
            timeout = max(0.0, min(next_beat, next_refresh) - loop.time())
            with suppress(TimeoutError):
                await asyncio.wait_for(sub.wakeup.wait(), timeout=timeout)
            for ev in sub.drain():
                if ev.id > sent_up_to:
                    sent_up_to = ev.id
                    yield ev.encode()
            now = loop.time()
            if now >= next_beat:
                next_beat = now + cfg.heartbeat_s
                yield hub.heartbeat({"ts": svc.clock.now().isoformat()}).encode()
            if now >= next_refresh:
                next_refresh = now + cfg.stream_symbols_refresh_s
                try:
                    fresh = await _user_symbols(user_id)
                except Exception as exc:
                    log.warning("stream symbol refresh failed", extra={"error": type(exc).__name__})
                    continue
                added = fresh - sub.symbols
                sub.set_symbols(fresh)
                if added:
                    for s, q in (await svc.get_quotes(sorted(added))).items():
                        ev = StreamEvent(
                            hub.next_id(), "quote", QuoteOut.from_quote(q).model_dump(mode="json"), None, s
                        )
                        yield ev.encode()
    finally:
        hub.unsubscribe(sub)


@router.get(
    "/stream", summary="Server-sent events: quote / alert / proposal / heartbeat", response_model=None
)
async def stream(
    request: Request,
    token: Annotated[str | None, Query(description="single-use token from POST /v1/stream/token")] = None,
    last_event_id: Annotated[str | None, Query(description="resume point when headers cannot be set")] = None,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    clock: Clock = request.app.state.clock
    user_id = await consume_stream_token(token or "", clock.now())
    if user_id is None:
        raise unauthenticated("Invalid or expired stream token")
    svc = get_service(request)
    if svc.hub.connections_for(user_id) >= MAX_STREAMS_PER_USER:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "Too many open streams",
            details={"retry_after_s": 5},
            headers={"Retry-After": "5"},
        )
    symbols = await _user_symbols(user_id)
    last_id = _parse_last_id(last_event_id_header, last_event_id)
    return StreamingResponse(
        _sse_events(svc, user_id, last_id, symbols),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
