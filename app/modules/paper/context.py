"""`EngineContext`: everything the paper engine needs from its environment (time, prices, market state).

Routes build it from the request (`EngineCtx` dependency); tests and jobs build it directly, which is how the
engine stays deterministic (injected `Clock` and `PriceSource`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, cast

from fastapi import Depends, Request

from app.core.clock import Clock
from app.core.db import get_db
from app.core.pricesource import PriceSource
from app.modules.paper.market import MarketState, effective_market_state
from app.modules.paper.pricing import DbPriceSource


@dataclass(frozen=True, slots=True)
class EngineContext:
    clock: Clock
    prices: PriceSource
    market: Callable[[datetime], MarketState]
    slippage_bps: Decimal = Decimal(5)

    def market_now(self) -> MarketState:
        return self.market(self.clock.now())


def resolve_price_source(app_state: Any, clock: Clock | None = None) -> PriceSource:
    """`app.state.price_source` (stream C) when installed, else the DB last-close fallback."""
    src = getattr(app_state, "price_source", None)
    if src is not None and hasattr(src, "get_last"):
        return cast(PriceSource, src)
    fallback = getattr(app_state, "_paper_db_prices", None)
    if fallback is None:
        fallback = DbPriceSource(get_db().pool, app_state, clock.now if clock else None)
        app_state._paper_db_prices = fallback
    return cast(PriceSource, fallback)


def build_context(app_state: Any) -> EngineContext:
    clock: Clock = app_state.clock
    settings = app_state.settings
    return EngineContext(
        clock=clock,
        prices=resolve_price_source(app_state, clock),
        market=lambda now: effective_market_state(app_state, now),
        slippage_bps=Decimal(str(settings.paper_slippage_bps)),
    )


def engine_context(request: Request) -> EngineContext:
    return build_context(request.app.state)


EngineCtx = Annotated[EngineContext, Depends(engine_context)]
