"""Feed supervisor: failover WS -> REST -> Finnhub -> cache, reconnect with exponential backoff + jitter.

Everything time-related is injected (`Clock`, `sleep`, `rng`) and the loop bodies (`ws_session_once`,
`poll_once`) are separately callable, so T-DATA-05 is testable without real sockets or sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.core.clock import Clock
from app.core.logging import get_logger
from app.modules.marketdata.sources.base import PollSource, StreamingSource, UpstreamError
from app.modules.marketdata.store import StoredQuote
from app.modules.marketdata.subscriptions import SubscriptionPlan

log = get_logger("app.marketdata.supervisor")

Tier = Literal["ws", "rest", "finnhub", "cache", "none"]
Sleeper = Callable[[float], Awaitable[None]]


class Backoff:
    """Exponential backoff with equal jitter: delay in [d/2, d], d = min(cap, base * 2^attempt)."""

    def __init__(self, base_s: float, cap_s: float, rng: random.Random) -> None:
        self._base = base_s
        self._cap = cap_s
        self._rng = rng
        self.attempt = 0

    def next(self) -> float:
        ceiling = min(self._cap, self._base * (2**self.attempt))
        self.attempt += 1
        return float(ceiling / 2 + self._rng.uniform(0, ceiling / 2))

    def reset(self) -> None:
        self.attempt = 0


@dataclass(frozen=True, slots=True)
class SupervisorTiming:
    poll_interval_s: float = 30.0
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    backoff_reset_after_s: float = 30.0


class FeedSupervisor:
    def __init__(
        self,
        *,
        clock: Clock,
        on_quote: Callable[[StoredQuote], None],
        plan: Callable[[], SubscriptionPlan],
        stream: StreamingSource | None = None,
        rest: PollSource | None = None,
        fallback: PollSource | None = None,
        timing: SupervisorTiming | None = None,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.clock = clock
        self._on_quote = on_quote
        self._plan = plan
        self.stream = stream
        self.rest = rest
        self.fallback = fallback
        self.timing = timing or SupervisorTiming()
        self._sleep = sleep
        self.backoff = Backoff(self.timing.backoff_base_s, self.timing.backoff_cap_s, rng or random.Random())  # noqa: S311
        self.ws_up = False
        self.tier: Tier = "none"
        self.last_error: str | None = None
        self.poll_wakeup = asyncio.Event()
        self._rr = 0  # round-robin cursor for the rate-limited fallback
        self._stopping = False

    # -- WebSocket ------------------------------------------------------
    async def ws_session_once(self) -> float:
        """One connection attempt. Returns the delay to wait before the next attempt."""
        assert self.stream is not None
        symbols = list(self._plan().ws)
        started = self.clock.monotonic()
        delivered = 0
        try:
            async for quote in self.stream.stream(symbols):
                if not self.ws_up:
                    log.info("websocket feed up", extra={"symbols": len(symbols)})
                self.ws_up = True
                self.tier = "ws"
                delivered += 1
                self._on_quote(quote)
        except UpstreamError as exc:
            self.last_error = str(exc)[:200]
            log.warning("websocket feed dropped", extra={"error": self.last_error})
        except (OSError, TimeoutError, ConnectionError, ValueError) as exc:
            self.last_error = type(exc).__name__
            log.warning("websocket feed dropped", extra={"error": self.last_error})
        finally:
            self.ws_up = False
            if self.tier == "ws":
                self.tier = "none"
            self.poll_wakeup.set()  # fail over to REST immediately
        if delivered and self.clock.monotonic() - started >= self.timing.backoff_reset_after_s:
            self.backoff.reset()
        return self.backoff.next()

    async def run_ws(self) -> None:
        while not self._stopping:
            if not self._plan().ws:
                await self._sleep(5.0)
                continue
            delay = await self.ws_session_once()
            log.info("websocket reconnect scheduled", extra={"delay_s": round(delay, 2)})
            await self._sleep(delay)

    # -- REST / Finnhub / cache ---------------------------------------
    def _symbols_to_poll(self) -> list[str]:
        plan = self._plan()
        # while the WS is healthy only the overflow needs REST; otherwise everything does
        return list(plan.rest) if self.ws_up else list(plan.all)

    async def poll_once(self) -> Tier:
        """Fetch what the WS is not covering. Order: REST -> Finnhub -> cache. Returns the serving tier."""
        symbols = self._symbols_to_poll()
        if not symbols:
            return self.tier
        got: dict[str, StoredQuote] = {}
        tier: Tier = "cache"
        if self.rest is not None:
            try:
                got = await self.rest.fetch(self._limit(self.rest, symbols))
                tier = "rest"
            except UpstreamError as exc:
                self.last_error = str(exc)[:200]
                log.warning("rest poll failed", extra={"source": self.rest.name, "error": self.last_error})
        if not got and self.fallback is not None:
            try:
                got = await self.fallback.fetch(self._limit(self.fallback, symbols))
                tier = "finnhub"
            except UpstreamError as exc:
                self.last_error = str(exc)[:200]
                log.warning(
                    "fallback poll failed", extra={"source": self.fallback.name, "error": self.last_error}
                )
        for q in got.values():
            self._on_quote(q)
        if not got:
            tier = "cache"  # nothing fresh: the store keeps serving the last quotes, which age to `stale`
        if not self.ws_up:
            self.tier = tier
        return tier

    def _limit(self, source: PollSource, symbols: Sequence[str]) -> list[str]:
        """Rate-limited sources (one HTTP call per symbol) get a rotating slice per poll."""
        chunk = getattr(source, "chunk", None)
        return self._next_chunk(symbols, int(chunk)) if chunk else list(symbols)

    def _next_chunk(self, symbols: Sequence[str], size: int) -> list[str]:
        n = max(1, size)
        start = self._rr % max(1, len(symbols))
        rotated = list(symbols[start:]) + list(symbols[:start])
        self._rr += n
        return rotated[:n]

    async def run_poll(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except Exception as exc:
                log.error("poll loop error", extra={"error": type(exc).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.poll_wakeup.wait(), timeout=self.timing.poll_interval_s)
            self.poll_wakeup.clear()

    def stop(self) -> None:
        self._stopping = True

    # -- reporting ------------------------------------------------------
    @property
    def feed_state(self) -> Literal["live", "delayed", "stale", "reconnecting"]:
        if self.ws_up or (self.stream is None and self.tier in ("rest", "finnhub")):
            return "live" if self.ws_up else "delayed"
        if self.tier in ("rest", "finnhub"):
            return "reconnecting"  # WS configured but down; serving delayed REST while we back off
        if self.tier == "cache":
            return "stale"
        return "reconnecting" if self.stream is not None else "delayed"
