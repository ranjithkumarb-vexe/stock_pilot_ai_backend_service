"""Revealed risk: 60-day realised volatility and beta of the user's *actual* portfolio -> a 1-5 tier (SPEC 10.3 D).

Computed locally from positions and `prices_daily` (static current weights applied to the last 60 daily
returns). Never raises on thin data: it returns `None` fields and flags instead (T-RISK-04 spirit).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

LOOKBACK = 60
MIN_OBS = 20
VOL_POINTS = ((6.0, 1.0), (11.0, 2.0), (16.0, 3.0), (24.0, 4.0), (36.0, 5.0))  # annualised vol %, tier
BETA_POINTS = ((0.4, 1.0), (0.7, 2.0), (1.0, 3.0), (1.3, 4.0), (1.7, 5.0))
VOL_WEIGHT = 0.6


@dataclass(frozen=True, slots=True)
class RevealedRisk:
    tier: float | None
    vol_pct: float | None
    beta: float | None
    n_obs: int
    flags: tuple[str, ...] = field(default=())


def _interp(x: float, points: Sequence[tuple[float, float]]) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return float(np.interp(x, xs, ys))  # clamps to the end values: tier stays within [1, 5]


def tier_from(vol_pct: float | None, beta: float | None) -> float | None:
    if vol_pct is None:
        return None
    t_vol = _interp(vol_pct, VOL_POINTS)
    if beta is None:
        return round(t_vol, 1)
    return round(VOL_WEIGHT * t_vol + (1 - VOL_WEIGHT) * _interp(beta, BETA_POINTS), 1)


def _returns(closes: Mapping[date, float]) -> dict[date, float]:
    days = sorted(closes)
    out: dict[date, float] = {}
    for prev, cur in itertools.pairwise(days):
        if closes[prev] > 0:
            out[cur] = closes[cur] / closes[prev] - 1.0
    return out


def compute_revealed_risk(
    values: Mapping[str, float],
    closes: Mapping[str, Mapping[date, float]],
    benchmark: Mapping[date, float] | None,
) -> RevealedRisk:
    """`values`: current market value per held symbol; `closes`: daily closes per symbol; `benchmark`: SPY."""
    flags: list[str] = []
    held = {s: v for s, v in values.items() if v > 0}
    if not held:
        return RevealedRisk(None, None, None, 0, ("no_positions",))
    total = sum(held.values())
    rets = {s: _returns(closes.get(s, {})) for s in held}
    missing = [s for s in held if not rets[s]]
    if missing:
        flags.append("missing_prices")
    usable = {s: v for s, v in held.items() if rets[s]}
    if not usable:
        return RevealedRisk(None, None, None, 0, (*flags, "no_price_history"))
    common = sorted(set.intersection(*(set(rets[s]) for s in usable)))[-LOOKBACK:]
    if len(common) < MIN_OBS:
        return RevealedRisk(None, None, None, len(common), (*flags, "insufficient_history"))
    wsum = sum(usable.values())
    port = np.array([sum((usable[s] / wsum) * rets[s][d] for s in usable) for d in common])
    if len(usable) < len(held):
        flags.append(f"coverage_{sum(usable.values()) / total:.2f}")
    sd = float(np.std(port, ddof=1))
    if sd < 1e-12:
        return RevealedRisk(None, None, None, len(common), (*flags, "zero_variance"))  # T-RISK-04
    vol = sd * float(np.sqrt(252)) * 100.0
    beta: float | None = None
    if benchmark:
        bret = _returns(benchmark)
        if all(d in bret for d in common):
            b = np.array([bret[d] for d in common])
            var_b = float(np.var(b, ddof=1))
            if var_b > 1e-14:
                beta = float(np.cov(port, b, ddof=1)[0, 1] / var_b)
            else:
                flags.append("zero_benchmark_variance")
        else:
            flags.append("benchmark_misaligned")
    else:
        flags.append("no_benchmark")
    return RevealedRisk(
        tier_from(vol, beta),
        round(vol, 2),
        None if beta is None else round(beta, 2),
        len(common),
        tuple(flags),
    )


def gap_state(
    *,
    stated: int | None,
    revealed: float | None,
    previous_days: int,
    previous_since: date | None,
    today: date,
) -> dict[str, object]:
    """Risk-gap bookkeeping (SPEC 10.3 G): |revealed - stated| >= 1 sustained for 14 days => reflective prompt.

    Never changes the stated tier; it only reports. `previous_*` come from the last stored `gap_days` feature.
    """
    if stated is None or revealed is None:
        return {"gap": None, "gap_days": previous_days, "since": previous_since, "flag": previous_days >= 14}
    gap = round(revealed - stated, 1)
    if abs(gap) < 1.0:
        return {"gap": gap, "gap_days": 0, "since": None, "flag": False}
    since = previous_since
    if since is None:
        from datetime import timedelta

        since = today - timedelta(days=previous_days) if previous_days > 0 else today
    days = max(0, (today - since).days)
    return {"gap": gap, "gap_days": days, "since": since, "flag": days >= 14}
