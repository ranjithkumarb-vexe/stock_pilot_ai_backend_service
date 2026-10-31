"""Minimal price-source interface. Stream C extends the implementations in `app/modules/marketdata`.

Domain code (paper engine, risk, alerts) depends on this Protocol only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

Session = Literal["pre", "regular", "post", "closed"]
Freshness = Literal["live", "delayed", "stale", "closed"]


class QuoteUnavailableError(LookupError):
    """No quote can be produced for the symbol (unknown symbol or no data yet)."""


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: Decimal
    ts: datetime
    source: str  # "simulated" | "alpaca" | "finnhub" | "cache" ...
    prev_close: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    session: Session = "regular"
    freshness: Freshness = "live"


@runtime_checkable
class PriceSource(Protocol):
    name: str

    async def get_last(self, symbol: str) -> Quote:
        """Latest quote; raises `QuoteUnavailableError` when none exists."""
        ...

    def subscribe(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        """Stream of quotes for `symbols` until the iterator is closed."""
        ...


class StaticPriceSource:
    """Dict-backed source for unit tests and for deterministic engine runs."""

    name = "static"

    def __init__(self, prices: dict[str, Decimal], ts: datetime, source: str = "static") -> None:
        self._prices = dict(prices)
        self._ts = ts
        self._source = source

    def set_price(self, symbol: str, price: Decimal, ts: datetime | None = None) -> None:
        self._prices[symbol] = price
        if ts is not None:
            self._ts = ts

    async def get_last(self, symbol: str) -> Quote:
        try:
            price = self._prices[symbol]
        except KeyError as exc:
            raise QuoteUnavailableError(symbol) from exc
        return Quote(symbol=symbol, price=price, ts=self._ts, source=self._source)

    async def get_many(self, symbols: Sequence[str]) -> dict[str, Quote]:
        return {s: await self.get_last(s) for s in symbols}

    async def subscribe(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        for s in symbols:
            yield await self.get_last(s)
