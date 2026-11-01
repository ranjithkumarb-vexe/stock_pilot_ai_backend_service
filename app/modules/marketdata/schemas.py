"""Response / request models for the market-data endpoints (CONTRACT: Quotes, Chart, Dev market controls)."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from pydantic import Field

from app.core.pricesource import Freshness, Quote, Session
from app.core.schemas import ApiModel, Pct, Price, ResponseModel

_PCT = Decimal("0.01")


def change_pct(price: Decimal, prev_close: Decimal | None) -> Decimal | None:
    if prev_close is None or prev_close <= 0:
        return None
    return ((price / prev_close - 1) * 100).quantize(_PCT, rounding=ROUND_HALF_EVEN)


class QuoteOut(ResponseModel):
    symbol: str
    price: Price
    prev_close: Price | None
    change_pct: Pct | None
    bid: Price | None = None
    ask: Price | None = None
    ts: datetime
    source: str
    session: Session
    freshness: Freshness

    @classmethod
    def from_quote(cls, q: Quote) -> QuoteOut:
        return cls(
            symbol=q.symbol,
            price=q.price,
            prev_close=q.prev_close,
            change_pct=change_pct(q.price, q.prev_close),
            bid=q.bid,
            ask=q.ask,
            ts=q.ts,
            source=q.source,
            session=q.session,
            freshness=q.freshness,
        )


class QuotesResponse(ResponseModel):
    items: list[QuoteOut]
    unavailable: list[str] = Field(default_factory=list, description="requested symbols with no quote yet")


class BarOut(ResponseModel):
    time: datetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int


class BarsResponse(ResponseModel):
    symbol: str
    interval: Literal["5m", "1d"]
    bars: list[BarOut]
    source: str


class MarketStatus(ResponseModel):
    open: bool
    session: Session
    as_of: datetime
    next_open: datetime
    next_close: datetime
    early_close: bool
    source: str = Field(description="active price source: simulated | alpaca | finnhub")
    simulated: bool
    feed_state: Literal["live", "delayed", "stale", "reconnecting"]
    override: bool | None = Field(description="dev market override (null = follow the NYSE calendar)")


class DevMarketRequest(ApiModel):
    open: bool | None = Field(
        description="true/false forces the session open/closed; null clears the override"
    )


class DevMarketResponse(ResponseModel):
    open: bool
    override: bool | None
    session: Session


class DevFeedRequest(ApiModel):
    state: Literal["live", "delayed", "stale", "reconnecting"]


class DevFeedResponse(ResponseModel):
    state: Literal["live", "delayed", "stale", "reconnecting"]
