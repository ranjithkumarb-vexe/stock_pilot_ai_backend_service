"""T-DATA-03 (holidays / half-days) and T-DATA-04 (DST, UTC storage)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.modules.marketdata.calendar import ET, NyseCalendar

cal = NyseCalendar()


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_regular_day_session_boundaries() -> None:
    d = cal.trading_day(date(2026, 10, 30))  # Friday, EDT
    assert d is not None and not d.early_close
    assert d.open_utc == utc(2026, 10, 30, 13, 30) and d.close_utc == utc(2026, 10, 30, 20, 0)
    assert cal.session_at(utc(2026, 10, 30, 7, 59)) == "closed"  # 03:59 ET
    assert cal.session_at(utc(2026, 10, 30, 8, 0)) == "pre"
    assert cal.session_at(utc(2026, 10, 30, 13, 29)) == "pre"
    assert cal.session_at(utc(2026, 10, 30, 13, 30)) == "regular"
    assert cal.session_at(utc(2026, 10, 30, 19, 59)) == "regular"
    assert cal.session_at(utc(2026, 10, 30, 20, 0)) == "post"
    assert cal.session_at(utc(2026, 10, 30, 23, 59)) == "post"
    assert cal.session_at(utc(2026, 10, 31, 0, 0)) == "closed"  # 20:00 ET: post-market over


def test_weekend_and_holiday_are_closed_all_day() -> None:
    assert cal.trading_day(date(2026, 10, 31)) is None  # Saturday
    assert cal.session_at(utc(2026, 10, 31, 15, 0)) == "closed"
    assert cal.trading_day(date(2026, 11, 26)) is None  # Thanksgiving
    assert cal.session_at(utc(2026, 11, 26, 15, 0)) == "closed"
    assert cal.trading_day(date(2026, 7, 3)) is None  # Independence Day observed
    assert cal.trading_day(date(2026, 12, 25)) is None


def test_half_day_closes_at_1pm_et() -> None:
    d = cal.trading_day(date(2026, 11, 27))  # day after Thanksgiving
    assert d is not None and d.early_close
    assert d.close_utc == utc(2026, 11, 27, 18, 0)  # 13:00 EST
    assert cal.session_at(utc(2026, 11, 27, 17, 59)) == "regular"
    assert cal.session_at(utc(2026, 11, 27, 18, 0)) == "post"
    assert cal.session_at(utc(2026, 11, 27, 22, 0)) == "closed"  # post ends 17:00 ET on a half day
    assert cal.is_early_close(utc(2026, 11, 27, 16, 0))
    assert not cal.is_early_close(utc(2026, 11, 25, 16, 0))


def test_dst_changes_utc_open_but_not_local_open() -> None:
    before = cal.trading_day(date(2026, 3, 6))  # EST (UTC-5)
    after = cal.trading_day(date(2026, 3, 9))  # EDT (UTC-4) after the Mar 8 switch
    assert before is not None and after is not None
    assert before.open_utc == utc(2026, 3, 6, 14, 30)
    assert after.open_utc == utc(2026, 3, 9, 13, 30)
    for d in (before, after):
        assert d.open_utc.astimezone(ET).strftime("%H:%M") == "09:30"
        assert d.open_utc.tzinfo is UTC  # stored in UTC
    # autumn: DST ends Sun 2026-11-01 -> Monday opens at 14:30Z again
    mon = cal.trading_day(date(2026, 11, 2))
    assert mon is not None and mon.open_utc == utc(2026, 11, 2, 14, 30)


def test_exchange_date_uses_new_york_not_utc() -> None:
    late_utc = utc(2026, 3, 10, 2, 0)  # 22:00 ET on Mar 9
    assert cal.exchange_date(late_utc) == date(2026, 3, 9)
    assert cal.session_at(late_utc) == "closed"


def test_next_open_and_close() -> None:
    fri_after_close = utc(2026, 10, 30, 21, 0)
    assert cal.next_open(fri_after_close) == utc(2026, 11, 2, 14, 30)  # Monday (EST)
    assert cal.next_open(utc(2026, 10, 30, 10, 0)) == utc(2026, 10, 30, 13, 30)
    assert cal.next_open(utc(2026, 11, 25, 22, 0)) == utc(2026, 11, 27, 14, 30)  # skips Thanksgiving
    assert cal.next_close(utc(2026, 11, 27, 15, 0)) == utc(2026, 11, 27, 18, 0)
    assert cal.next_close(utc(2026, 11, 25, 22, 0)) == utc(2026, 11, 27, 18, 0)


def test_last_session_and_neighbours() -> None:
    assert cal.last_session(utc(2026, 11, 1, 12, 0)).day == date(2026, 10, 30)  # Sunday -> Friday
    assert cal.last_session(utc(2026, 11, 2, 12, 0)).day == date(2026, 10, 30)  # Monday pre-open
    assert cal.last_session(utc(2026, 11, 2, 15, 0)).day == date(2026, 11, 2)
    assert cal.previous_trading_day(date(2026, 11, 27)).day == date(2026, 11, 25)
    assert cal.next_trading_day(date(2026, 11, 25)).day == date(2026, 11, 27)


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027])
def test_every_trading_day_is_a_weekday_and_regular_session_is_6_5h_or_less(year: int) -> None:
    d = date(year, 1, 1)
    while d.year == year:
        td = cal.trading_day(d)
        if td is not None:
            assert d.weekday() < 5
            assert td.close_utc - td.open_utc <= timedelta(hours=6, minutes=30)
        d += timedelta(days=1)
