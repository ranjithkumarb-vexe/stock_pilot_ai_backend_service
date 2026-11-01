"""Market-data runtime configuration.

`MARKET_DATA_SOURCE`, `ALPACA_*` and `FINNHUB_KEY` come from `Settings`. The tuning knobs below are read from
`MARKETDATA_*` environment variables (not part of `Settings`, which is owned by the foundation) and default to
the SPEC 6.4 values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings

SourceName = Literal["simulated", "alpaca", "finnhub"]


def _f(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _b(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class MarketDataConfig:
    source: SourceName = "simulated"
    alpaca_key_id: str = ""
    alpaca_secret: str = ""
    finnhub_key: str = ""
    # simulator
    sim_seed: int = 42
    sim_tick_s: float = 1.0
    sim_sigma: float = 0.0004  # per-tick log-return std (4 bp)
    # failover / polling
    poll_interval_s: float = 30.0  # REST poll; 30 s = 2 calls/min per batch, far below 200/min
    finnhub_interval_s: float = 5.0  # per symbol round robin keeps < 60 calls/min (see FinnhubSource)
    ws_symbol_cap: int = 30  # Alpaca Basic plan WebSocket symbol limit
    ws_warn_at: int = 25  # SPEC 6.4 rule 1: warn when the set grows past 25
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    backoff_reset_after_s: float = 30.0  # a WS session this long counts as healthy and resets the backoff
    subscription_refresh_s: float = 60.0
    # freshness (SPEC 6.4 rule 3)
    live_max_age_s: float = 5.0
    stale_after_s: float = 900.0
    # SSE
    heartbeat_s: float = 15.0
    ring_size: int = 2000
    subscriber_queue: int = 500
    stream_symbols_refresh_s: float = 30.0
    autostart: bool = True

    @classmethod
    def from_settings(cls, settings: Settings, env: Mapping[str, str] | None = None) -> MarketDataConfig:
        e = os.environ if env is None else env
        return cls(
            source=settings.market_data_source,
            alpaca_key_id=settings.alpaca_key_id,
            alpaca_secret=settings.alpaca_secret,
            finnhub_key=settings.finnhub_key,
            sim_seed=int(_f(e, "MARKETDATA_SIM_SEED", 42)),
            sim_tick_s=_f(e, "MARKETDATA_SIM_TICK_S", 1.0),
            sim_sigma=_f(e, "MARKETDATA_SIM_SIGMA", 0.0004),
            poll_interval_s=_f(e, "MARKETDATA_POLL_INTERVAL_S", 30.0),
            finnhub_interval_s=_f(e, "MARKETDATA_FINNHUB_INTERVAL_S", 5.0),
            heartbeat_s=_f(e, "MARKETDATA_HEARTBEAT_S", 15.0),
            autostart=_b(e, "MARKETDATA_AUTOSTART", True),
        )
