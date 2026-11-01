"""Freshness state machine (SPEC 6.4 rule 3, T-DATA-02). Pure; no I/O, no clock.

* outside the regular session           -> `closed` (last close is shown; takes precedence over everything)
* streaming quote (WS / simulator)      -> `live` while < 5 s old, `delayed` (WS gap) up to 15 min, then `stale`
* polled quote (REST / Finnhub)         -> `delayed` while the last successful poll is <= 15 min old, then `stale`
* a dev/UI feed override (`feed_state`) -> forces live / delayed / stale for every quote in a regular session;
  `reconnecting` is reported as `delayed` on quotes (the state itself is exposed by /market/status).

Age is measured from the time the quote was *received* (a 15-min-delayed REST quote has an old exchange
timestamp by construction, so exchange time cannot define staleness for it).
"""

from __future__ import annotations

from typing import Literal

from app.core.pricesource import Freshness, Session

FeedState = Literal["live", "delayed", "stale", "reconnecting"]
QuoteKind = Literal["stream", "polled"]

LIVE_MAX_AGE_S = 5.0
STALE_AFTER_S = 900.0

_FEED_TO_FRESHNESS: dict[FeedState, Freshness] = {
    "live": "live",
    "delayed": "delayed",
    "reconnecting": "delayed",
    "stale": "stale",
}


def compute_freshness(
    *,
    session: Session,
    kind: QuoteKind,
    age_s: float,
    feed_state: FeedState | None = None,
    live_max_age_s: float = LIVE_MAX_AGE_S,
    stale_after_s: float = STALE_AFTER_S,
) -> Freshness:
    if session != "regular":
        return "closed"
    if feed_state is not None:
        return _FEED_TO_FRESHNESS[feed_state]
    if age_s > stale_after_s:
        return "stale"
    if kind == "stream" and age_s < live_max_age_s:
        return "live"
    return "delayed"


def blocks_market_fills(freshness: Freshness) -> bool:
    """T-DATA-02: a stale quote blocks market-order fills."""
    return freshness == "stale"


def allows_move_alerts(freshness: Freshness) -> bool:
    """T-DATA-02: unusual-move alerts are suppressed while stale (and while the market is closed)."""
    return freshness in {"live", "delayed"}
