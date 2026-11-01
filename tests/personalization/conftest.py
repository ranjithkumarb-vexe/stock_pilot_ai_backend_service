"""DB helpers for the personalization tests (admin connection; bypasses RLS to arrange data)."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
SECTORS = {
    "QAT1": "Technology",
    "QAT2": "Technology",
    "QAD1": "Consumer Staples",
    "QAD2": "Healthcare",
    "SPY": "Index",
}


DEFAULT_END = NOW.date() - timedelta(days=1)


class World:
    """Arranges users, prices, positions, fills and events for one test; cleans up after itself."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.symbols: set[str] = set()
        self._portfolios: dict[UUID, UUID] = {}

    async def tickers(self) -> None:
        for sym, sector in SECTORS.items():
            await self.conn.execute(
                "insert into tickers (symbol, name, sector, in_universe) values ($1, $1, $2, $3) "
                "on conflict (symbol) do update set sector = excluded.sector",
                sym, sector, sym != "SPY",
            )  # fmt: skip
            self.symbols.add(sym)

    async def prices(
        self, symbol: str, returns: list[float], end: date = DEFAULT_END, start_px: float = 100.0
    ) -> None:
        await self.tickers()
        days = [end - timedelta(days=len(returns) - i) for i in range(len(returns) + 1)]
        px = start_px
        for i, d in enumerate(days):
            if i:
                px *= 1 + returns[i - 1]
            await self.conn.execute(
                "insert into prices_daily (symbol, date, open, high, low, close, volume) values ($1, $2, $3, $3, $3, $3, 1000) "
                "on conflict (symbol, date) do update set close = excluded.close",
                symbol, d, px,
            )  # fmt: skip

    async def market(self, aggressive: bool, n: int = 80) -> list[float]:
        rng = random.Random(11)
        spy = [rng.gauss(0.0004, 0.008) for _ in range(n)]
        await self.prices("SPY", spy, start_px=600.0)
        mult = 2.2 if aggressive else 0.35
        for sym in ("QAT1", "QAT2", "QAD1", "QAD2"):
            idio = random.Random(sym)
            await self.prices(sym, [mult * r + idio.gauss(0, 0.004) for r in spy])
        return spy

    async def profile(
        self, uid: UUID, tier: int, experience: str = "Intermediate", goals: tuple[str, ...] = ("Growth",)
    ) -> None:
        await self.conn.execute(
            "insert into profiles (user_id, risk_tier_stated, experience, goals) values ($1, $2, $3, $4) "
            "on conflict (user_id) do update set risk_tier_stated = excluded.risk_tier_stated",
            uid, tier, experience, list(goals),
        )  # fmt: skip

    async def portfolio(self, uid: UUID) -> UUID:
        if uid not in self._portfolios:
            self._portfolios[uid] = await self.conn.fetchval(
                "insert into portfolios (user_id, name, starting_cash) values ($1, 'Main', 100000) returning id",
                uid,
            )
        return self._portfolios[uid]

    async def position(self, uid: UUID, symbol: str, qty: float, avg_cost: float) -> None:
        pid = await self.portfolio(uid)
        await self.conn.execute(
            "insert into positions (portfolio_id, symbol, user_id, qty, avg_cost) values ($1, $2, $3, $4, $5) "
            "on conflict (portfolio_id, symbol) do update set qty = excluded.qty",
            pid, symbol, uid, qty, avg_cost,
        )  # fmt: skip

    async def fill(self, uid: UUID, symbol: str, side: str, qty: float, price: float, ts: datetime) -> None:
        pid = await self.portfolio(uid)
        oid = await self.conn.fetchval(
            "insert into orders (portfolio_id, user_id, symbol, side, type, qty, status, idempotency_key) "
            "values ($1, $2, $3, $4, 'market', $5, 'filled', $6) returning id",
            pid, uid, symbol, side, qty, str(uuid4()),
        )  # fmt: skip
        await self.conn.execute(
            "insert into fills (order_id, user_id, portfolio_id, symbol, qty, price, filled_at) values ($1, $2, $3, $4, $5, $6, $7)",
            oid, uid, pid, symbol, qty, price, ts,
        )  # fmt: skip

    async def event(
        self,
        uid: UUID,
        etype: str,
        ts: datetime,
        entity: dict[str, Any] | None = None,
        props: dict[str, Any] | None = None,
    ) -> UUID:
        eid = uuid4()
        await self.conn.execute(
            "insert into events (event_id, user_id, ts, session_id, type, entity, props) values ($1, $2, $3, 's', $4, $5, $6)",
            eid, uid, ts, etype, entity or {}, props or {},
        )  # fmt: skip
        return eid

    async def proposal(
        self, uid: UUID, symbol: str, status: str, created: datetime, side: str = "sell"
    ) -> None:
        pid = await self.portfolio(uid)
        await self.conn.execute(
            "insert into proposals (user_id, portfolio_id, type, symbol, payload, status, created_at) "
            "values ($1, $2, 'trade', $3, $4, $5, $6)",
            uid, pid, symbol, {"side": side}, status, created,
        )  # fmt: skip

    async def feature(self, uid: UUID, key: str) -> Any:
        return await self.conn.fetchval(
            "select value from user_features where user_id = $1 and key = $2", uid, key
        )

    async def set_feature(self, uid: UUID, key: str, value: Any) -> None:
        await self.conn.execute(
            "insert into user_features (user_id, key, value, confidence) values ($1, $2, $3, 0.7) "
            "on conflict (user_id, key) do update set value = excluded.value",
            uid, key, value,
        )  # fmt: skip

    async def interest(self, uid: UUID, dim: str, weight: float) -> None:
        await self.conn.execute(
            "insert into user_interest_vector (user_id, dim, weight, baseline) values ($1, $2, $3, $3) "
            "on conflict (user_id, dim) do update set weight = excluded.weight, baseline = excluded.baseline",
            uid, dim, weight,
        )  # fmt: skip

    async def cleanup(self) -> None:
        # users are removed (cascade) by make_user; reference rows we created are ours alone
        if self.symbols:
            await self.conn.execute(
                "delete from prices_daily where symbol = any($1::text[])", sorted(self.symbols)
            )


@pytest_asyncio.fixture
async def world(db_admin: Any) -> AsyncIterator[World]:
    w = World(db_admin)
    await w.tickers()
    try:
        yield w
    finally:
        await w.cleanup()


@pytest.fixture
def recompute(settings: Any, test_database: str) -> Callable[..., Awaitable[Any]]:
    """Run `recompute_features` as the user through a real RLS connection (no app needed)."""
    from app.core.db import Database, set_database
    from app.modules.personalization.recompute import recompute_features

    async def _run(uid: UUID, now: datetime = NOW) -> Any:
        db = Database(settings)
        await db.open()
        set_database(db)
        try:
            return await recompute_features(uid, now=now)
        finally:
            set_database(None)
            await db.close()

    return _run
