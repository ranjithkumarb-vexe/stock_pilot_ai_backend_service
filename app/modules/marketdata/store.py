"""In-process quote store (ADR-004: Redis optional)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from app.modules.marketdata.freshness import QuoteKind


@dataclass(frozen=True, slots=True)
class StoredQuote:
    symbol: str
    price: Decimal
    ts: datetime  # exchange / generation time
    received_at: datetime  # when WE got it (drives freshness)
    source: str  # "simulated" | "alpaca_iex_ws" | "alpaca_rest_delayed" | "finnhub"
    kind: QuoteKind = "stream"
    bid: Decimal | None = None
    ask: Decimal | None = None
    prev_close: Decimal | None = None


class QuoteStore:
    def __init__(self) -> None:
        self._quotes: dict[str, StoredQuote] = {}
        self._prev_close: dict[str, Decimal] = {}

    def set_prev_close(self, symbol: str, value: Decimal) -> None:
        self._prev_close[symbol] = value

    def prev_close(self, symbol: str) -> Decimal | None:
        return self._prev_close.get(symbol)

    def put(self, quote: StoredQuote) -> bool:
        """Store `quote`; returns False (and keeps the existing one) when it is older than what we hold."""
        current = self._quotes.get(quote.symbol)
        if current is not None and quote.ts < current.ts:
            return False
        if quote.prev_close is None:
            known = self._prev_close.get(quote.symbol)
            if known is not None:
                quote = replace(quote, prev_close=known)
        elif quote.symbol not in self._prev_close:
            self._prev_close[quote.symbol] = quote.prev_close
        if current is not None and quote.bid is None and quote.ask is None and quote.source == current.source:
            quote = replace(quote, bid=current.bid, ask=current.ask)
        self._quotes[quote.symbol] = quote
        return True

    def get(self, symbol: str) -> StoredQuote | None:
        return self._quotes.get(symbol)

    def symbols(self) -> list[str]:
        return sorted(self._quotes)

    def snapshot(self, symbols: Iterable[str] | None = None) -> list[StoredQuote]:
        if symbols is None:
            return [self._quotes[s] for s in sorted(self._quotes)]
        return [self._quotes[s] for s in symbols if s in self._quotes]
