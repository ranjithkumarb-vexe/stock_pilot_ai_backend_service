"""NYSE trading calendar (holidays, half-days, DST) on top of `pandas_market_calendars`.

Everything is stored and compared in UTC; only `ET` is used to decide which *trading date* an instant belongs
to. Extended hours follow the usual convention: pre-market 04:00-open ET, post-market close-20:00 ET
(close+4 h, i.e. 17:00 ET on a 13:00 half-day).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from app.core.pricesource import Session

ET = ZoneInfo("America/New_York")
PRE_MARKET = timedelta(hours=5, minutes=30)  # 04:00 ET before a 09:30 open
POST_MARKET = timedelta(hours=4)  # 16:00 -> 20:00 ET
_NORMAL_CLOSE_SPAN = timedelta(hours=6, minutes=30)
_MAX_LOOKAHEAD_DAYS = 14


@dataclass(frozen=True, slots=True)
class TradingDay:
    day: date  # exchange (ET) calendar date
    open_utc: datetime
    close_utc: datetime
    early_close: bool

    @property
    def pre_start_utc(self) -> datetime:
        return self.open_utc - PRE_MARKET

    @property
    def post_end_utc(self) -> datetime:
        return self.close_utc + POST_MARKET


class NyseCalendar:
    """Thin cached wrapper; one instance per process is enough."""

    def __init__(self) -> None:
        self._cal = mcal.get_calendar("NYSE")
        self._days: dict[date, TradingDay | None] = {}
        self._loaded: set[tuple[int, int]] = set()

    # -- loading -------------------------------------------------------
    def _load_month(self, year: int, month: int) -> None:
        if (year, month) in self._loaded:
            return
        first = date(year, month, 1)
        last = (date(year + (month == 12), (month % 12) + 1, 1)) - timedelta(days=1)
        sched = self._cal.schedule(start_date=first, end_date=last)
        early = set(self._cal.early_closes(sched).index.date) if len(sched) else set()
        for d in (first + timedelta(days=i) for i in range((last - first).days + 1)):
            self._days[d] = None
        for idx, row in sched.iterrows():
            d = idx.date()
            self._days[d] = TradingDay(
                day=d,
                open_utc=row["market_open"].to_pydatetime().astimezone(UTC),
                close_utc=row["market_close"].to_pydatetime().astimezone(UTC),
                early_close=d in early,
            )
        self._loaded.add((year, month))

    def trading_day(self, d: date) -> TradingDay | None:
        """The trading day for exchange date `d`, or None (weekend / holiday)."""
        self._load_month(d.year, d.month)
        return self._days[d]

    # -- queries -------------------------------------------------------
    @staticmethod
    def exchange_date(ts: datetime) -> date:
        return ts.astimezone(ET).date()

    def session_at(self, ts: datetime) -> Session:
        day = self.trading_day(self.exchange_date(ts))
        if day is None:
            return "closed"
        if day.open_utc <= ts < day.close_utc:
            return "regular"
        if day.pre_start_utc <= ts < day.open_utc:
            return "pre"
        if day.close_utc <= ts < day.post_end_utc:
            return "post"
        return "closed"

    def is_regular_open(self, ts: datetime) -> bool:
        return self.session_at(ts) == "regular"

    def is_trading_day(self, d: date) -> bool:
        return self.trading_day(d) is not None

    def next_trading_day(self, d: date) -> TradingDay:
        cur = d
        for _ in range(_MAX_LOOKAHEAD_DAYS):
            cur += timedelta(days=1)
            found = self.trading_day(cur)
            if found is not None:
                return found
        raise RuntimeError("no trading day within lookahead")  # unreachable for NYSE

    def previous_trading_day(self, d: date) -> TradingDay:
        cur = d
        for _ in range(_MAX_LOOKAHEAD_DAYS):
            cur -= timedelta(days=1)
            found = self.trading_day(cur)
            if found is not None:
                return found
        raise RuntimeError("no trading day within lookback")

    def next_open(self, ts: datetime) -> datetime:
        """First regular-session open strictly after `ts`."""
        today = self.trading_day(self.exchange_date(ts))
        if today is not None and ts < today.open_utc:
            return today.open_utc
        return self.next_trading_day(self.exchange_date(ts)).open_utc

    def next_close(self, ts: datetime) -> datetime:
        """The close of the regular session that is open at `ts`, else of the next one."""
        today = self.trading_day(self.exchange_date(ts))
        if today is not None and ts < today.close_utc:
            return today.close_utc
        return self.next_trading_day(self.exchange_date(ts)).close_utc

    def last_session(self, ts: datetime) -> TradingDay:
        """The regular session in progress at `ts`, else the most recent completed one."""
        today = self.trading_day(self.exchange_date(ts))
        if today is not None and ts >= today.open_utc:
            return today
        return self.previous_trading_day(self.exchange_date(ts))

    def is_early_close(self, ts: datetime) -> bool:
        day = self.trading_day(self.exchange_date(ts))
        return bool(day and day.early_close)
