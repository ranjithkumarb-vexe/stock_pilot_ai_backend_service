"""SSE fan-out hub: ring buffer for `Last-Event-ID` replay + per-connection coalescing queues.

* quote events are coalesced per symbol (a slow client only ever misses *intermediate* ticks);
* alert / proposal events are never coalesced (bounded deque per connection; overflow drops the oldest and is
  logged - the client resyncs on reconnect via REST);
* every event carries a monotonically increasing integer id (global per process);
* a user only ever receives quotes for the symbols in their own watchlist+holdings and their own alerts /
  proposals (`user_id` match) - events for other users are never queued for this connection.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

log = get_logger("app.marketdata.hub")

USER_EVENT_TYPES = frozenset({"alert", "proposal"})


@dataclass(frozen=True, slots=True)
class StreamEvent:
    id: int
    type: str  # quote | alert | proposal | heartbeat
    data: dict[str, Any]
    user_id: UUID | None = None  # None = visible per `symbol` rules
    symbol: str | None = None

    def encode(self) -> bytes:
        payload = json.dumps(self.data, separators=(",", ":"), default=str)
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n".encode()


@dataclass(slots=True, eq=False)
class Subscriber:
    user_id: UUID
    symbols: set[str]
    max_other: int = 500
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    _quotes: dict[str, StreamEvent] = field(default_factory=dict)
    _other: deque[StreamEvent] = field(default_factory=deque)
    dropped: int = 0

    def offer(self, ev: StreamEvent) -> None:
        if ev.type == "quote":
            if ev.symbol in self.symbols:
                self._quotes[ev.symbol or ""] = ev
                self.wakeup.set()
        elif ev.user_id == self.user_id:
            if len(self._other) >= self.max_other:
                self._other.popleft()
                self.dropped += 1
            self._other.append(ev)
            self.wakeup.set()

    def set_symbols(self, symbols: Iterable[str]) -> None:
        self.symbols = set(symbols)
        for gone in [s for s in self._quotes if s not in self.symbols]:
            del self._quotes[gone]

    def drain(self) -> list[StreamEvent]:
        out = [*self._quotes.values(), *self._other]
        self._quotes.clear()
        self._other.clear()
        self.wakeup.clear()
        out.sort(key=lambda e: e.id)
        return out


class StreamHub:
    def __init__(self, ring_size: int = 2000, subscriber_queue: int = 500) -> None:
        self._ring: deque[StreamEvent] = deque(maxlen=ring_size)
        self._subs: set[Subscriber] = set()
        self._seq = 0
        self._queue = subscriber_queue

    # -- ids / publishing ------------------------------------------------
    def next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _publish(self, ev: StreamEvent) -> StreamEvent:
        self._ring.append(ev)
        for sub in self._subs:
            sub.offer(ev)
        return ev

    def publish_quote(self, symbol: str, data: dict[str, Any]) -> StreamEvent:
        return self._publish(StreamEvent(self.next_id(), "quote", data, None, symbol))

    def publish_user_event(self, user_id: UUID, event_type: str, data: dict[str, Any]) -> StreamEvent:
        """Deliver an `alert` / `proposal` event to one user (called by the alert and proposal modules)."""
        if event_type not in USER_EVENT_TYPES:
            raise ValueError(f"unsupported user event type {event_type!r}")
        return self._publish(StreamEvent(self.next_id(), event_type, data, user_id, None))

    def heartbeat(self, data: dict[str, Any]) -> StreamEvent:
        """Not stored in the ring and not fanned out: heartbeats are per connection."""
        return StreamEvent(self.next_id(), "heartbeat", data)

    # -- subscribers --------------------------------------------------
    def subscribe(self, user_id: UUID, symbols: Iterable[str]) -> Subscriber:
        sub = Subscriber(user_id=user_id, symbols=set(symbols), max_other=self._queue)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def connections_for(self, user_id: UUID) -> int:
        return sum(1 for s in self._subs if s.user_id == user_id)

    # -- replay -------------------------------------------------------
    def replay(self, user_id: UUID, symbols: set[str], last_id: int) -> list[StreamEvent]:
        """Events a reconnecting client missed: every alert/proposal for the user, and the latest quote of each
        of their symbols, with id > `last_id`."""
        latest: dict[str, StreamEvent] = {}
        other: list[StreamEvent] = []
        for ev in self._ring:
            if ev.id <= last_id:
                continue
            if ev.type == "quote":
                if ev.symbol in symbols:
                    latest[ev.symbol or ""] = ev
            elif ev.user_id == user_id:
                other.append(ev)
        return sorted([*latest.values(), *other], key=lambda e: e.id)
