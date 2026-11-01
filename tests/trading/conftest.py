"""Fixtures for the trading-domain API tests (streams A): a seeded database and an app whose clock, market
state and price source are fully controlled."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.core.clock import FakeClock
from app.core.db import get_db, init_connection
from app.core.personas import PERSONAS
from app.modules.paper.pricing import DbPriceSource
from scripts.seed import seed

# the background order sweeper is driven explicitly by the tests
os.environ["PAPER_SWEEP_INTERVAL_S"] = "0"

PERSONA_IDS = [p.user_id for p in PERSONAS.values()]


@pytest_asyncio.fixture(scope="session")
async def seed_now(test_database: str) -> AsyncIterator[datetime]:
    """Seed universe + 3 personas once per session (real `now`, so dev JWTs and the FakeClock agree)."""
    now = datetime.now(UTC)
    conn = await asyncpg.connect(test_database)
    await init_connection(conn)
    try:
        await seed(conn, now=now, reset=True)
        yield now
        await conn.execute("delete from users where id = any($1::uuid[])", PERSONA_IDS)
    finally:
        await conn.close()


@pytest.fixture
def reseed(test_database: str, seed_now: datetime) -> Callable[..., Any]:
    """`await reseed("priya")` rebuilds a persona from scratch (tests that mutate a persona call this first)."""

    async def _run(*keys: str) -> None:
        conn = await asyncpg.connect(test_database)
        await init_connection(conn)
        try:
            await seed(conn, now=seed_now, reset=True, personas=list(keys) or None)
        finally:
            await conn.close()

    return _run


@pytest_asyncio.fixture
async def tclient(
    app_factory: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]], seed_now: datetime
) -> AsyncIterator[httpx.AsyncClient]:
    """App with a FakeClock at the seed time, market OPEN, and the DB last-close price source (stream C's
    simulated source, if installed, is replaced so numbers are exactly the seeded design prices)."""
    clock = FakeClock(start=seed_now)
    async with app_factory(clock=clock, rate_limit_enabled=False) as client:
        app = client.app  # type: ignore[attr-defined]
        app.state.market_override = True
        app.state.price_source = DbPriceSource(get_db().pool, app.state, clock.now)
        client.clock = clock  # type: ignore[attr-defined]
        yield client
