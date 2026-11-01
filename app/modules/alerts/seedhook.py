"""Used by `scripts/seed.py` (and tests): evaluate the persona's alerts inside the seed transaction.

Breach alerts come from the real risk engine; the seeded rows (`scripts/seed.py` inserts the design's alert set)
that describe the same breaches are adopted, not duplicated. Priced from the (uncommitted) seeded daily closes
with the market closed, so no unusual-move alert is generated from synthetic prices.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.core.clock import FakeClock
from app.core.db import Connection
from app.modules.alerts.service import EvalContext, evaluate_user
from app.modules.paper.context import EngineContext
from app.modules.paper.market import MarketState
from app.modules.paper.pricing import ConnPriceSource


async def refresh_for_seed(conn: Connection, user_id: UUID, now: datetime) -> int:
    """Number of alerts the engine created for `user_id` (0 when the seeded set already covers them)."""
    ctx = EngineContext(
        clock=FakeClock(start=now),
        prices=ConnPriceSource(conn),
        market=lambda _t: MarketState(False, "closed", None, None),
        slippage_bps=Decimal(5),
    )
    result = await evaluate_user(conn, user_id, EvalContext(now=now, ctx=ctx))
    return len(result.created)
