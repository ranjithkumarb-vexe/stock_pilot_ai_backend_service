"""NYSE market state for the paper engine (independent of stream C's market-data module).

`effective_market_state(app_state, now)` honours the dev override `app.state.market_override`
(None = follow the calendar, True = open, False = closed) which stream C's dev controls set. It is read
defensively: anything other than a bool is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas_market_calendars as mcal

from app.core.pricesource import Session


@dataclass(frozen=True, slots=True)
class MarketState:
    is_open: bool
    session: Session
    next_open: datetime | None  # next regular-session open strictly after `now` (None if unknown)
    close_at: datetime | None  # close of the regular session that is open now / opens next
    override: bool | None = None


class MarketCalendar:
    """Cached wrapper over `pandas_market_calendars` NYSE."""

    def __init__(self) -> None:
        self._cal = mcal.get_calendar("NYSE")
        self._cache: dict[date, list[tuple[datetime, datetime]]] = {}

    def _sessions(self, around: datetime) -> list[tuple[datetime, datetime]]:
        key = around.astimezone(UTC).date()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        sched = self._cal.schedule(start_date=key - timedelta(days=6), end_date=key + timedelta(days=14))
        rows = [
            (r["market_open"].to_pydatetime(), r["market_close"].to_pydatetime()) for _, r in sched.iterrows()
        ]
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[key] = rows
        return rows

    def calendar_state(self, now: datetime) -> MarketState:
        now = now.astimezone(UTC)
        rows = self._sessions(now)
        for o, c in rows:
            if o <= now < c:
                nxt = next((o2 for o2, _ in rows if o2 > now), None)
                return MarketState(True, "regular", nxt, c)
        nxt_o = next(((o, c) for o, c in rows if o > now), None)
        session: Session = "closed"
        for o, c in rows:
            if o - timedelta(hours=5, minutes=30) <= now < o:
                session = "pre"
            elif c <= now < c + timedelta(hours=4):
                session = "post"
        return MarketState(False, session, nxt_o[0] if nxt_o else None, nxt_o[1] if nxt_o else None)

    def session_close_after(self, when: datetime) -> datetime | None:
        """Close of the regular session containing `when`, else of the next one."""
        st = self.calendar_state(when)
        return st.close_at


_CALENDAR: MarketCalendar | None = None


def calendar() -> MarketCalendar:
    global _CALENDAR
    if _CALENDAR is None:
        _CALENDAR = MarketCalendar()
    return _CALENDAR


def read_override(app_state: Any) -> bool | None:
    value = getattr(app_state, "market_override", None)
    return value if isinstance(value, bool) else None


def effective_market_state(app_state: Any, now: datetime) -> MarketState:
    base = calendar().calendar_state(now)
    override = read_override(app_state)
    if override is None:
        return base
    if override:
        return MarketState(True, "regular", base.next_open, base.close_at or now + timedelta(hours=6), True)
    return MarketState(False, "closed", base.next_open, base.close_at, False)
