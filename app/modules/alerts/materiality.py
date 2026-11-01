"""Deterministic alert materiality, 0-1 (SPEC 7.7):

    materiality = 0.40 * event_type_weight + 0.25 * magnitude + 0.25 * exposure + 0.10 * recency

* event type: 8-K item weights (results / agreements / officers / restatement / impairments are high), fixed
  weights for the other alert types;
* magnitude: size of the event (|z| for moves, share of high-impact items for filings, overage for breaches);
* exposure: position weight of the ticker (a watchlist-only ticker scores lower);
* recency: exponential decay with a 24 h scale.
Pure functions: no I/O, no clock.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

W_TYPE, W_MAGNITUDE, W_EXPOSURE, W_RECENCY = 0.40, 0.25, 0.25, 0.10

# 8-K item numbers (SEC Form 8-K). >= 0.8 counts as "high" in the spec.
ITEM_WEIGHTS: dict[str, float] = {
    "4.02": 0.95,  # non-reliance on previously issued financials (restatement)
    "2.02": 0.85,  # results of operations
    "2.06": 0.85,  # material impairments
    "1.01": 0.80,  # material definitive agreement
    "5.02": 0.80,  # departure / appointment of officers
    "1.02": 0.70,  # termination of a material agreement
    "2.04": 0.70,  # triggering events that accelerate an obligation
    "2.05": 0.65,  # exit or disposal costs
    "3.01": 0.65,  # delisting notice
    "1.03": 0.90,  # bankruptcy
    "4.01": 0.55,  # change of certifying accountant
    "2.01": 0.60,  # completion of acquisition or disposition
    "5.01": 0.55,  # change in control
    "8.01": 0.40,  # other events
    "7.01": 0.30,  # Reg FD disclosure
    "5.07": 0.25,  # shareholder vote results
    "5.03": 0.25,  # bylaw amendments
    "3.03": 0.30,  # material modification to rights of holders
    "9.01": 0.10,  # financial statements and exhibits
}
DEFAULT_ITEM_WEIGHT = 0.35
HIGH_ITEM = 0.8

TYPE_WEIGHTS: dict[str, float] = {
    "breach": 1.0,
    "concentration": 0.7,
    "move": 0.55,
    "earnings": 0.5,
    "mirror": 0.35,
}


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def split_items(items: str | Iterable[str] | None) -> list[str]:
    """`"2.02,9.01"` -> `["2.02", "9.01"]`."""
    if not items:
        return []
    raw = items.split(",") if isinstance(items, str) else list(items)
    return [x.strip() for x in raw if x and x.strip()]


def filing_type_weight(items: Iterable[str]) -> float:
    ws = [ITEM_WEIGHTS.get(i, DEFAULT_ITEM_WEIGHT) for i in items]
    return max(ws) if ws else DEFAULT_ITEM_WEIGHT


def filing_magnitude(items: Iterable[str]) -> float:
    items = list(items)
    high = sum(1 for i in items if ITEM_WEIGHTS.get(i, DEFAULT_ITEM_WEIGHT) >= HIGH_ITEM)
    return clamp01(0.3 + 0.35 * high) if items else 0.2


def move_magnitude(z: float, k: float) -> float:
    """0 at |z| == k, 1 at |z| == k + 2."""
    return clamp01((abs(z) - k) / 2.0)


def exposure(weight_pct: float | None, *, held: bool) -> float:
    """Held positions score 0.5-1.0 by weight (saturating at 25 %); watchlist-only tickers 0.2."""
    if not held or weight_pct is None:
        return 0.2
    return 0.5 + 0.5 * clamp01(weight_pct / 25.0)


def recency(age_minutes: float) -> float:
    return clamp01(math.exp(-max(0.0, age_minutes) / (24 * 60)))


def materiality(type_weight: float, magnitude: float, exposure_: float, recency_: float) -> float:
    score = (
        W_TYPE * clamp01(type_weight)
        + W_MAGNITUDE * clamp01(magnitude)
        + W_EXPOSURE * clamp01(exposure_)
        + W_RECENCY * clamp01(recency_)
    )
    return round(clamp01(score), 4)


def breach_materiality(overage_pct: float) -> float:
    """Rule breaches are critical; materiality 0.90 at the limit rising to 1.0 at 20 points over."""
    return round(0.90 + 0.10 * clamp01(overage_pct / 20.0), 4)
