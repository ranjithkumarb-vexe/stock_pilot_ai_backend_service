"""Upstream source interfaces used by the feed supervisor.

Domain code (paper engine, risk, alerts) only sees `app.core.pricesource.PriceSource`, which the
`MarketDataService` implements on top of whichever of these sources is configured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from app.modules.marketdata.store import StoredQuote


class UpstreamError(Exception):
    """A source could not deliver (network error, auth failure, rate limit, bad payload)."""


@runtime_checkable
class StreamingSource(Protocol):
    """Push source (WebSocket / simulator). `stream` runs until the connection drops, then raises
    `UpstreamError` (or returns when closed on purpose)."""

    name: str

    def stream(self, symbols: Sequence[str]) -> AsyncIterator[StoredQuote]: ...


@runtime_checkable
class PollSource(Protocol):
    """Pull source (REST). Returns the symbols it could quote; raises `UpstreamError` when it is down."""

    name: str

    async def fetch(self, symbols: Sequence[str]) -> dict[str, StoredQuote]: ...
