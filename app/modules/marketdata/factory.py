"""Builds the `MarketDataService` for the configured `MARKET_DATA_SOURCE` (falls back to the simulator when
a real source has no credentials - the UI then shows `source: "simulated"`)."""

from __future__ import annotations

from typing import Any

from app.core.clock import Clock
from app.core.logging import get_logger
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.service import MarketDataService
from app.modules.marketdata.sources.alpaca import AlpacaRest, AlpacaStream
from app.modules.marketdata.sources.finnhub import FinnhubRest
from app.modules.marketdata.sources.simulated import SimulatedSource
from app.modules.marketdata.supervisor import FeedSupervisor, SupervisorTiming

log = get_logger("app.marketdata.factory")


def build_service(cfg: MarketDataConfig, clock: Clock, state: Any) -> MarketDataService:
    source = cfg.source
    if source == "alpaca" and not (cfg.alpaca_key_id and cfg.alpaca_secret):
        log.warning("MARKET_DATA_SOURCE=alpaca without ALPACA_KEY_ID/ALPACA_SECRET: using the simulator")
        source = "simulated"
    if source == "finnhub" and not cfg.finnhub_key:
        log.warning("MARKET_DATA_SOURCE=finnhub without FINNHUB_KEY: using the simulator")
        source = "simulated"

    if source == "simulated":
        sim = SimulatedSource(None, clock, seed=cfg.sim_seed, sigma=cfg.sim_sigma, tick_s=cfg.sim_tick_s)
        return MarketDataService(cfg=cfg, clock=clock, state=state, simulated=sim)

    timing = SupervisorTiming(
        poll_interval_s=cfg.poll_interval_s,
        backoff_base_s=cfg.backoff_base_s,
        backoff_cap_s=cfg.backoff_cap_s,
        backoff_reset_after_s=cfg.backoff_reset_after_s,
    )
    holder: list[MarketDataService] = []

    def plan() -> Any:
        return holder[0].current_plan()

    def on_quote(sq: Any) -> None:
        holder[0].on_quote(sq)

    closers = []
    fallback = None
    if cfg.finnhub_key:
        fh = FinnhubRest(cfg.finnhub_key, clock)
        fallback = fh
        closers.append(fh.aclose)
    if source == "alpaca":
        rest = AlpacaRest(cfg.alpaca_key_id, cfg.alpaca_secret, clock)
        closers.append(rest.aclose)
        supervisor = FeedSupervisor(
            clock=clock,
            on_quote=on_quote,
            plan=plan,
            stream=AlpacaStream(cfg.alpaca_key_id, cfg.alpaca_secret, clock),
            rest=rest,
            fallback=fallback,
            timing=timing,
        )
    else:  # finnhub only: polled, no stream; 3 symbols per 5 s keeps us well under 60 calls/min
        timing = SupervisorTiming(
            poll_interval_s=cfg.finnhub_interval_s,
            backoff_base_s=cfg.backoff_base_s,
            backoff_cap_s=cfg.backoff_cap_s,
            backoff_reset_after_s=cfg.backoff_reset_after_s,
        )
        supervisor = FeedSupervisor(
            clock=clock,
            on_quote=on_quote,
            plan=plan,
            stream=None,
            rest=fallback,
            fallback=None,
            timing=timing,
        )
    svc = MarketDataService(cfg=cfg, clock=clock, state=state, supervisor=supervisor)
    svc.closers.extend(closers)
    holder.append(svc)
    return svc
