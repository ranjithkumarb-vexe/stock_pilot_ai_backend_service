"""Injectable time. Domain logic takes a `Clock` so tests never sleep."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC wall-clock time."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds (for rate limits / timeouts)."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Deterministic clock; `advance()` moves both wall and monotonic time."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 30, 14, 30, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware datetime")
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float = 0.0, **delta: float) -> None:
        td = timedelta(seconds=seconds, **delta)
        self._now += td
        self._mono += td.total_seconds()

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware datetime")
        self._mono += (value - self._now).total_seconds()
        self._now = value

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
