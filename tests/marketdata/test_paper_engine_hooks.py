"""Stream A's paper engine consumes `app.state.price_source` / `app.state.market_override` (read-only check)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.clock import FakeClock
from app.modules.paper.context import build_context

pytestmark = pytest.mark.db


async def test_engine_context_uses_the_market_data_service(
    md_app: Callable[..., Any], make_user: Any, auth_headers: Any
) -> None:
    uid = await make_user()
    h = auth_headers(uid)
    clock = FakeClock(datetime(2026, 11, 1, 15, 0, tzinfo=UTC))  # Sunday
    async with md_app(clock=clock) as m:
        ctx = build_context(m.app.state)
        assert ctx.prices is m.app.state.price_source
        assert ctx.market_now().is_open is False
        assert (await ctx.prices.get_last("AAPL")).freshness == "closed"
        await m.client.post("/v1/dev/market", json={"open": True}, headers=h)
        assert ctx.market_now().is_open is True and ctx.market_now().override is True
        assert (await ctx.prices.get_last("AAPL")).session == "regular"
        await m.client.post("/v1/dev/feed", json={"state": "stale"}, headers=h)
        assert (
            await ctx.prices.get_last("AAPL")
        ).freshness == "stale"  # what blocks market orders (T-DATA-02)
        await m.client.post("/v1/dev/feed", json={"state": "live"}, headers=h)
        assert (await ctx.prices.get_last("AAPL")).freshness == "live"
