"""Chart bars. `1d` comes from `prices_daily`; `5m` is synthesised from the simulator and labelled as such.

The synthetic 5-minute path of a session is a deterministic function of `(seed, symbol, date)`: it opens at the
same level as the live simulator (same first RNG draw), mean-reverts like it, and is bridged so the last close
equals the current simulated price. It is NOT market data.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.core.db import Connection
from app.modules.marketdata.calendar import NyseCalendar, TradingDay
from app.modules.marketdata.sources.simulated import SOURCE_LABEL, money

BUCKET = timedelta(minutes=5)
_TICKS_PER_BUCKET = 300
_MAX_SESSIONS = 8


@dataclass(frozen=True, slots=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def _base_volume(symbol: str) -> int:
    return 20_000 + int(hashlib.sha256(symbol.encode()).hexdigest()[:6], 16) % 380_000


def _session_bars(
    *,
    symbol: str,
    day: TradingDay,
    seed: int,
    anchor: float,
    end: datetime,
    sigma: float,
    theta: float,
    last_price: float | None,
) -> list[Bar]:
    day_str = day.day.isoformat()
    open_rng = random.Random(f"{seed}:{symbol}:{day.open_utc.date().isoformat()}")  # noqa: S311
    open_dev = open_rng.gauss(0.0, 0.006)  # identical first draw to the live simulator's opening gap
    log_dev = open_dev
    rng = random.Random(f"{seed}:{symbol}:{day_str}:bars")  # noqa: S311
    # aggregate the per-tick OU process to 5-minute steps keeping the same stationary spread
    stationary_sd = sigma / math.sqrt(1 - (1 - theta) ** 2)
    a = (1 - theta) ** _TICKS_PER_BUCKET
    sigma_b = stationary_sd * math.sqrt(1 - a * a)
    theta_b = 1 - a
    stop = min(day.close_utc, end)
    starts: list[datetime] = []
    t = day.open_utc
    while t < stop:
        starts.append(t)
        t += BUCKET
    if not starts:
        return []
    levels: list[float] = []
    for _ in starts:
        log_dev = log_dev * (1 - theta_b) + sigma_b * rng.gauss(0.0, 1.0)
        levels.append(log_dev)
    if last_price is not None and last_price > 0:
        shift = math.log(last_price / anchor) - levels[-1]
        n = len(levels)
        levels = [lv + shift * (i + 1) / n for i, lv in enumerate(levels)]
    base_vol = _base_volume(symbol)
    bars: list[Bar] = []
    prev = anchor * math.exp(open_dev)
    for start, lv in zip(starts, levels, strict=True):
        close = anchor * math.exp(lv)
        hi = max(prev, close) * (1 + abs(rng.gauss(0.0, 0.0006)))
        lo = min(prev, close) * (1 - abs(rng.gauss(0.0, 0.0006)))
        vol = int(base_vol * rng.uniform(0.5, 1.5))
        bars.append(
            Bar(start, float(money(prev)), float(money(hi)), float(money(lo)), float(money(close)), vol)
        )
        prev = close
    return bars


def synth_bars_5m(
    *,
    symbol: str,
    calendar: NyseCalendar,
    now: datetime,
    anchor: float,
    last_price: float | None,
    seed: int = 42,
    sigma: float = 0.0004,
    theta: float = 0.002,
    limit: int = 78,
) -> list[Bar]:
    """Most recent `limit` 5-minute bars ending at the in-progress / last completed regular session."""
    days: list[TradingDay] = []
    cur: TradingDay = calendar.last_session(now)
    available = 0
    for _ in range(_MAX_SESSIONS):
        days.append(cur)
        span = min(cur.close_utc, now) - cur.open_utc
        available += max(0, math.ceil(span / BUCKET))
        if available >= limit:
            break
        cur = calendar.previous_trading_day(cur.day)
    days.reverse()
    out: list[Bar] = []
    for i, day in enumerate(days):
        is_latest = i == len(days) - 1
        out.extend(
            _session_bars(
                symbol=symbol,
                day=day,
                seed=seed,
                anchor=anchor,
                end=now if is_latest else day.close_utc,
                sigma=sigma,
                theta=theta,
                last_price=last_price if is_latest else None,
            )
        )
    return out[-limit:]


async def daily_bars(conn: Connection, symbol: str, limit: int) -> list[Bar]:
    rows = await conn.fetch(
        "select date, open, high, low, close, volume from prices_daily where symbol = $1 "
        "order by date desc limit $2",
        symbol,
        limit,
    )
    bars: list[Bar] = []
    for r in reversed(rows):
        d: date = r["date"]
        close = float(r["close"])
        bars.append(
            Bar(
                time=datetime(d.year, d.month, d.day, tzinfo=UTC),
                open=float(r["open"] if r["open"] is not None else close),
                high=float(r["high"] if r["high"] is not None else close),
                low=float(r["low"] if r["low"] is not None else close),
                close=close,
                volume=int(r["volume"] or 0),
            )
        )
    return bars


BARS_SOURCE_5M = SOURCE_LABEL
BARS_SOURCE_1D = "prices_daily"
