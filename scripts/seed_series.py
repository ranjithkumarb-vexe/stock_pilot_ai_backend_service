"""Deterministic SIMULATED market history for the seed (labelled `source = 'simulated'`).

* `trading_sessions` - NYSE sessions via pandas_market_calendars.
* `generate_prices` - ~120 daily OHLCV bars per symbol: a one-factor model (beta x SPY + idiosyncratic noise),
  anchored so the LAST close equals the design price and the last-day change equals the design `ch`.
* `calibrated_equity_curve` - a portfolio equity path whose period return, annualised volatility and beta vs
  SPY hit the design's persona stats exactly (sample statistics over the series), with the search over
  noise seeds picking the candidate whose current/max drawdown is closest to the design's.

Everything is seeded (`numpy.random.RandomState`, stable across numpy versions): same input -> same output.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas_market_calendars as mcal

from scripts.seed_data import SeedTicker

N_SESSIONS = 120
TRADING_DAYS = 252
SPY_PERIOD_RETURN = 0.119  # design persona.stats.spy
SPY_DAILY_DRIFT = 0.0004
SPY_DAILY_VOL = 0.0085

_NYSE = mcal.get_calendar("NYSE")


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    open_utc: datetime
    close_utc: datetime


def trading_sessions(now: datetime, n: int = N_SESSIONS) -> list[Session]:
    """The last `n` NYSE sessions whose close is <= `now` (oldest first)."""
    end = now.astimezone(UTC).date()
    sched = _NYSE.schedule(start_date=end - timedelta(days=int(n * 1.6) + 20), end_date=end)
    out = [
        Session(idx.date(), row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime())
        for idx, row in sched.iterrows()
        if row["market_close"].to_pydatetime() <= now
    ]
    if len(out) < n:
        raise RuntimeError("calendar window too short")
    return out[-n:]


def _rng(label: str) -> np.random.RandomState:
    return np.random.RandomState(zlib.crc32(label.encode()) & 0x7FFFFFFF)


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _dec(x: float) -> Decimal:
    return Decimal(f"{x:.4f}")


def spy_returns(n_returns: int, last_change: float) -> np.ndarray:
    """SPY daily simple returns; compounds to exactly `SPY_PERIOD_RETURN`, last day = `last_change`."""
    rng = _rng("SPY:returns")
    r = rng.normal(SPY_DAILY_DRIFT, SPY_DAILY_VOL, n_returns)
    r[-1] = last_change
    head = r[:-1]
    target = np.log1p(SPY_PERIOD_RETURN) - np.log1p(last_change)
    c = _solve_shift(head[None, :], np.array([target]))[0]
    r[:-1] = head + c
    return r


def _solve_shift(r: np.ndarray, target_log: np.ndarray) -> np.ndarray:
    """Per-row constant c with sum(log1p(r + c)) == target_log (bisection; sum is increasing in c)."""
    lo = np.full(r.shape[0], -0.2)
    hi = np.full(r.shape[0], 0.2)
    for _ in range(80):
        mid = (lo + hi) / 2
        val = np.log1p(np.clip(r + mid[:, None], -0.99, None)).sum(axis=1)
        up = val < target_log
        lo = np.where(up, mid, lo)
        hi = np.where(up, hi, mid)
    return np.asarray((lo + hi) / 2, dtype=float)


def generate_prices(
    tickers: list[SeedTicker], benchmark: SeedTicker, sessions: list[Session]
) -> dict[str, list[Bar]]:
    """Daily bars for every ticker + the benchmark (same session dates)."""
    n = len(sessions)
    spy_r = spy_returns(n - 1, float(benchmark.ch) / 100)
    out: dict[str, list[Bar]] = {}
    for t in [benchmark, *tickers]:
        if t.symbol == benchmark.symbol:
            r = spy_r
        else:
            rng = _rng(f"{t.symbol}:returns")
            beta = float(t.beta)
            sigma = 0.0070 + 0.0035 * beta
            r = beta * spy_r + rng.normal(0.0001, sigma, n - 1)
            r[-1] = float(t.ch) / 100  # design day change
        closes = np.empty(n)
        closes[-1] = float(t.px)
        for i in range(n - 1, 0, -1):
            closes[i - 1] = closes[i] / (1.0 + r[i - 1])
        rng2 = _rng(f"{t.symbol}:ohlc")
        gaps = rng2.normal(0.0, 0.0025, n)
        wick_h = np.abs(rng2.normal(0.0, 0.004, n))
        wick_l = np.abs(rng2.normal(0.0, 0.004, n))
        base_vol = 2_000_000 + 40_000_000 / (1.0 + float(t.px) / 100.0)
        vols = rng2.lognormal(0.0, 0.25, n) * base_vol
        bars: list[Bar] = []
        for i, s in enumerate(sessions):
            c = closes[i]
            o = c if i == 0 else closes[i - 1] * (1.0 + gaps[i])
            hi = max(o, c) * (1.0 + wick_h[i])
            lo = min(o, c) * (1.0 - wick_l[i])
            bars.append(Bar(s.day, _dec(o), _dec(hi), _dec(lo), _dec(c), int(vols[i])))
        out[t.symbol] = bars
    return out


@dataclass(frozen=True, slots=True)
class CurveTarget:
    ytd_pct: float
    vol_pct: float
    beta: float
    dd_pct: float
    maxdd_pct: float


def curve_stats(values: np.ndarray, spy: np.ndarray) -> dict[str, float]:
    """Period return / vol / beta / drawdowns of an equity path (the same maths the portfolio API uses)."""
    r = values[1:] / values[:-1] - 1.0
    rs = spy[1:] / spy[:-1] - 1.0
    peak = np.maximum.accumulate(values)
    dd = 1.0 - values / peak
    cov = float(np.cov(r, rs, ddof=1)[0, 1])
    return {
        "ytd_pct": float(values[-1] / values[0] - 1.0) * 100,
        "vol_pct": float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)) * 100,
        "beta": cov / float(rs.var(ddof=1)),
        "dd_pct": float(dd[-1]) * 100,
        "maxdd_pct": float(dd.max()) * 100,
    }


def calibrated_equity_curve(
    spy_closes: np.ndarray, target: CurveTarget, end_equity: float, label: str, candidates: int = 6000
) -> np.ndarray:
    """Equity path (len(spy_closes) values, last == `end_equity`) matching `target`'s ytd/vol/beta exactly and
    its drawdowns as closely as the best of `candidates` deterministic noise draws allows."""
    rs = spy_closes[1:] / spy_closes[:-1] - 1.0
    n = rs.shape[0]
    var_s = float(rs.var(ddof=1))
    sigma_d2 = (target.vol_pct / 100.0) ** 2 / TRADING_DAYS
    idio_var = sigma_d2 - target.beta**2 * var_s
    if idio_var <= 0:
        raise ValueError("target volatility is below beta x benchmark volatility")
    rng = _rng(f"curve:{label}")
    noise = rng.standard_normal((candidates, n))
    x = np.column_stack([np.ones(n), rs])
    proj = x @ np.linalg.inv(x.T @ x) @ x.T
    noise = noise - noise @ proj  # orthogonal to a constant and to the benchmark => exact beta & vol
    noise = noise / noise.std(axis=1, ddof=1, keepdims=True) * np.sqrt(idio_var)
    ret = target.beta * rs[None, :] + noise
    shift = _solve_shift(ret, np.full(candidates, np.log1p(target.ytd_pct / 100.0)))
    ret = ret + shift[:, None]
    curves = np.concatenate([np.ones((candidates, 1)), np.cumprod(1.0 + ret, axis=1)], axis=1)
    peak = np.maximum.accumulate(curves, axis=1)
    dd = 1.0 - curves / peak
    score = np.abs(dd[:, -1] * 100 - target.dd_pct) + np.abs(dd.max(axis=1) * 100 - target.maxdd_pct)
    best = curves[int(np.argmin(score))]
    return np.asarray(best / best[-1] * end_equity, dtype=float)
