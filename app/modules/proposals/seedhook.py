"""Used by `scripts/seed.py`: generate the persona proposals inside the seed transaction."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.core.clock import FakeClock
from app.core.db import Connection
from app.modules.paper.context import EngineContext
from app.modules.paper.market import MarketState
from app.modules.paper.pricing import ConnPriceSource
from app.modules.proposals import service


async def refresh_for_seed(conn: Connection, user_id: UUID, now: datetime) -> int:
    """Number of proposals created for `user_id`, priced from the (uncommitted) seeded daily closes."""
    ctx = EngineContext(
        clock=FakeClock(start=now),
        prices=ConnPriceSource(conn),
        market=lambda t: MarketState(False, "closed", None, None),
        slippage_bps=Decimal(5),
    )
    result = await service.refresh(conn, user_id, ctx)
    return int(result["created"])
