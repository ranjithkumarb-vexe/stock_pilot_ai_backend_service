"""Adaptive unusual-move threshold and notification fatigue (SPEC 10.6.6, 10.3 B; T-ALERT-03). Pure.

* `k` starts at 2.0 and moves by 0.25: up when fatigue > 0.7, down when the click-through rate > 0.5; bounded [1.5, 3.0].
* fatigue = (dismissed + snoozed) / acted-on, ctr = opened / sent, both over non-critical alerts of the last 7 days.
* The delivery floor for non-critical alerts follows `k` (higher k -> higher minimum materiality).
* Critical alerts (rule breaches) ignore both: they are always delivered, pinned, and cannot be dismissed.
"""

from __future__ import annotations

from dataclasses import dataclass

K_START = 2.0
K_MIN = 1.5
K_MAX = 3.0
K_STEP = 0.25
FATIGUE_HIGH = 0.7
CTR_HIGH = 0.5
MIN_ACTED = 3  # evidence needed before the threshold moves
BASE_MIN_MATERIALITY = 0.30
MIN_MATERIALITY_LO, MIN_MATERIALITY_HI = 0.20, 0.50
MIN_MATERIALITY_PER_K = 0.20
WINDOW_DAYS = 7


@dataclass(frozen=True, slots=True)
class Counts:
    sent: int = 0
    opened: int = 0
    dismissed: int = 0
    snoozed: int = 0

    @property
    def acted(self) -> int:
        return self.opened + self.dismissed + self.snoozed


def fatigue_index(c: Counts) -> float:
    """Share of acted-on alerts the user pushed away (dismiss / snooze). 0 when nothing was acted on."""
    return 0.0 if c.acted == 0 else (c.dismissed + c.snoozed) / c.acted


def click_through(c: Counts) -> float:
    return 0.0 if c.sent == 0 else c.opened / c.sent


def clamp_k(k: float) -> float:
    return max(K_MIN, min(K_MAX, round(k, 4)))


def next_k(k: float, c: Counts) -> float:
    """One adaptation step after a user action."""
    if c.acted < MIN_ACTED:
        return clamp_k(k)
    if fatigue_index(c) > FATIGUE_HIGH:
        return clamp_k(k + K_STEP)
    if click_through(c) > CTR_HIGH:
        return clamp_k(k - K_STEP)
    return clamp_k(k)


def min_materiality(k: float) -> float:
    """Delivery floor for non-critical alerts: 0.30 at k = 2.0, 0.50 at k = 3.0, 0.20 at k = 1.5 (clamped)."""
    raw = BASE_MIN_MATERIALITY + MIN_MATERIALITY_PER_K * (k - K_START)
    return round(max(MIN_MATERIALITY_LO, min(MIN_MATERIALITY_HI, raw)), 4)
