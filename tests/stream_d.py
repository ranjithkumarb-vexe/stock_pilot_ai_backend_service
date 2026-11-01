"""Shared arrangement helpers for the stream D (ranker / feed / alerts) tests.

`Env` writes through the owner connection (RLS bypassed) to build users, prices, positions, alerts and proposals;
the code under test runs through real RLS connections (`user_tx`). Everything uses its own `Q*` tickers.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.core.clock import FakeClock
from app.core.db import Database, get_db, init_connection, set_database
from app.core.personas import PERSONAS
from app.core.pricesource import Freshness, Quote, QuoteUnavailableError, Session
from app.modules.paper.context import EngineContext
from app.modules.paper.market import MarketState
from app.modules.paper.pricing import DbPriceSource
from app.modules.risk import default_rules_for_tier
from scripts.seed import seed

os.environ.setdefault("ALERTS_AUTOSTART", "0")
os.environ.setdefault("PAPER_SWEEP_INTERVAL_S", "0")
os.environ.setdefault("ALERTS_EDGAR_LIVE", "0")

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)  # a Friday, 11:00 ET: regular session
SECTORS = {
    "QT1": ("Technology", "Semiconductors"),
    "QT2": ("Technology", "Software"),
    "QT3": ("Technology", "Hardware"),
    "QS1": ("Consumer Staples", "Beverages"),
    "QS2": ("Consumer Staples", "Household Products"),
    "QH1": ("Healthcare", "Pharmaceuticals"),
    "QX1": ("Consumer Staples", "Tobacco"),
    "QSPY": ("Index", "ETF"),
}
OPEN = MarketState(True, "regular", None, None)
CLOSED = MarketState(False, "closed", None, None)


class FixedPrices:
    """Controlled `PriceSource`: price/prev_close/freshness per symbol."""

    name = "fixed"

    def __init__(self, ts: datetime = NOW) -> None:
        self.q: dict[str, Quote] = {}
        self.ts = ts

    def set(
        self,
        symbol: str,
        price: float,
        prev_close: float | None = None,
        *,
        freshness: Freshness = "live",
        session: Session = "regular",
    ) -> None:
        self.q[symbol] = Quote(
            symbol, Decimal(str(price)), self.ts, "test_feed",
            None if prev_close is None else Decimal(str(prev_close)), None, None, session, freshness,
        )  # fmt: skip

    async def get_last(self, symbol: str) -> Quote:
        try:
            return self.q[symbol]
        except KeyError as exc:
            raise QuoteUnavailableError(symbol) from exc

    def subscribe(self, symbols: Iterable[str]) -> Any:  # pragma: no cover - protocol only
        raise NotImplementedError


def engine_ctx(
    prices: FixedPrices, clock: FakeClock | None = None, state: MarketState = OPEN
) -> EngineContext:
    clk = clock or FakeClock(start=NOW)
    return EngineContext(clock=clk, prices=prices, market=lambda _now: state)


class Env:
    def __init__(self, admin: Any, make_user: Any) -> None:
        self.c = admin
        self._make_user = make_user
        self.portfolios: dict[UUID, UUID] = {}

    async def tickers(self) -> None:
        for sym, (sector, industry) in SECTORS.items():
            await self.c.execute(
                "insert into tickers (symbol, name, sector, industry, in_universe, cik) values ($1, $2, $3, $4, $5, $6) "
                "on conflict (symbol) do update set sector = excluded.sector, industry = excluded.industry",
                sym, f"{sym} Corp", sector, industry, sym != "QSPY", f"{abs(hash(sym)) % 900000 + 1000:010d}",
            )  # fmt: skip

    async def prices(
        self, symbol: str, returns: list[float], end: date | None = None, start_px: float = 100.0
    ) -> float:
        """Daily closes ending the day before `end` (default: the day before NOW). Returns the last close."""
        end = end or (NOW.date())
        days = [end - timedelta(days=len(returns) - i) for i in range(len(returns) + 1)]
        px = start_px
        for i, d in enumerate(days):
            if i:
                px *= 1 + returns[i - 1]
            await self.c.execute(
                "insert into prices_daily (symbol, date, open, high, low, close, volume) "
                "values ($1, $2, $3, $3, $3, $3, 1000) on conflict (symbol, date) do update set close = excluded.close",
                symbol, d, px,
            )  # fmt: skip
        return px

    async def user(
        self,
        *,
        tier: int = 3,
        cash: float = 100_000,
        rules: dict[str, float] | None = None,
        esg: tuple[str, ...] = (),
        goals: tuple[str, ...] = ("Growth",),
        name: str = "Dana Tester",
    ) -> UUID:
        uid: UUID = await self._make_user(display_name=name)
        await self.c.execute(
            "insert into profiles (user_id, risk_tier_stated, experience, goals, esg_exclusions) "
            "values ($1, $2, 'Intermediate', $3, $4)",
            uid, tier, list(goals), list(esg),
        )  # fmt: skip
        base = default_rules_for_tier(tier).to_dict()
        for k, v in {**base, **(rules or {})}.items():
            await self.c.execute(
                "insert into risk_rules (user_id, rule_key, value, source) values ($1, $2, $3, 'default')",
                uid,
                k,
                v,
            )
        pid = await self.c.fetchval(
            "insert into portfolios (user_id, name, starting_cash) values ($1, 'Main', $2) returning id",
            uid,
            cash,
        )
        self.portfolios[uid] = pid
        await self.c.execute(
            "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount) values ($1, $2, $3, 'deposit', $4)",
            pid, uid, NOW - timedelta(days=90), cash,
        )  # fmt: skip
        return uid

    async def hold(self, uid: UUID, symbol: str, qty: float, avg_cost: float = 100.0) -> None:
        await self.c.execute(
            "insert into positions (portfolio_id, symbol, user_id, qty, avg_cost) values ($1, $2, $3, $4, $5) "
            "on conflict (portfolio_id, symbol) do update set qty = excluded.qty, avg_cost = excluded.avg_cost",
            self.portfolios[uid], symbol, uid, qty, avg_cost,
        )  # fmt: skip

    async def watch(self, uid: UUID, symbol: str) -> None:
        await self.c.execute(
            "insert into watchlist_items (user_id, symbol) values ($1, $2) on conflict do nothing",
            uid,
            symbol,
        )

    async def alert(
        self,
        uid: UUID,
        *,
        type: str = "filing",
        symbol: str | None = "QT1",
        materiality: float = 0.6,
        critical: bool = False,
        state: str = "new",
        title: str | None = None,
        age_min: float = 30,
        payload: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> UUID:
        created = NOW - timedelta(minutes=age_min)
        body = {
            "title": title or f"{symbol} {type} alert",
            "body": "b",
            "source_label": "test",
            **(payload or {}),
        }
        aid: UUID = await self.c.fetchval(
            "insert into alerts (user_id, symbol, type, materiality, critical, payload, dedupe_key, state, created_at, "
            "delivered_at) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9) returning id",
            uid, symbol, type, materiality, critical, body, key or f"t:{uuid4()}", state, created,
        )  # fmt: skip
        return aid

    async def proposal(
        self,
        uid: UUID,
        *,
        symbol: str = "QT1",
        side: str = "sell",
        qty: int = 10,
        kind: str = "trade",
        rule: str = "pos",
        status: str = "pending",
        age_min: float = 60,
    ) -> UUID:
        created = NOW - timedelta(minutes=age_min)
        payload = {
            "rule": rule, "kind": kind, "side": side if kind == "trade" else None, "title": f"{rule} proposal {symbol}",
            "body": "body", "sources": ["Risk engine"], "suggested_qty": qty if kind == "trade" else 0,
            "current_weight_pct": 12.0, "after_weight_pct": 9.0, "cap_pct": 10.0,
        }  # fmt: skip
        pid: UUID = await self.c.fetchval(
            "insert into proposals (user_id, portfolio_id, type, symbol, payload, rationale, rules_checked, status, "
            "created_at, expires_at) values ($1, $2, $3, $4, $5, '[\"r\"]'::jsonb, '[]'::jsonb, $6, $7, $8) returning id",
            uid, self.portfolios[uid], kind, symbol, payload, status, created, created + timedelta(hours=24),
        )  # fmt: skip
        return pid

    async def interest(self, uid: UUID, dim: str, weight: float) -> None:
        await self.c.execute(
            "insert into user_interest_vector (user_id, dim, weight, baseline) values ($1, $2, $3, $3) "
            "on conflict (user_id, dim) do update set weight = excluded.weight",
            uid, dim, weight,
        )  # fmt: skip

    async def arm(self, uid: UUID, arm: str, alpha: float, beta: float, context: str = "") -> None:
        await self.c.execute(
            "insert into bandit_arms (user_id, arm, context, alpha, beta) values ($1, $2, $3, $4, $5) "
            "on conflict (user_id, arm, context) do update set alpha = excluded.alpha, beta = excluded.beta",
            uid, arm, context, alpha, beta,
        )  # fmt: skip

    async def event(
        self, uid: UUID, etype: str, ts: datetime | None = None, entity: dict[str, Any] | None = None,
        props: dict[str, Any] | None = None,
    ) -> None:  # fmt: skip
        await self.c.execute(
            "insert into events (event_id, user_id, ts, session_id, type, entity, props) values ($1, $2, $3, 's', $4, $5, $6)",
            uuid4(), uid, ts or NOW - timedelta(minutes=5), etype, entity or {}, props or {},
        )  # fmt: skip

    async def cleanup(self) -> None:
        """Remove every reference row this suite created (the tickers too: other suites count the universe)."""
        syms = [
            r["symbol"]
            for r in await self.c.fetch("select symbol from tickers where symbol ~ '^Q[A-Z0-9]+$'")
        ]
        for table in (
            "prices_daily",
            "raw_documents",
            "watchlist_items",
            "positions",
            "alerts",
            "proposals",
            "orders",
            "fills",
            "briefs",
        ):
            await self.c.execute(f"delete from {table} where symbol = any($1::text[])", syms)
        await self.c.execute("delete from tickers where symbol = any($1::text[])", syms)


@pytest_asyncio.fixture
async def env(db_admin: Any, make_user: Any) -> AsyncIterator[Env]:
    e = Env(db_admin, make_user)
    await e.tickers()
    try:
        yield e
    finally:
        await e.cleanup()


@pytest_asyncio.fixture
async def db(settings: Any, test_database: str) -> AsyncIterator[Database]:
    """The app database layer (pool as `stockpilot_app`) installed globally, for code that calls `user_tx`."""
    d = Database(settings)
    await d.open()
    set_database(d)
    try:
        yield d
    finally:
        set_database(None)
        await d.close()


@pytest.fixture
def prices() -> FixedPrices:
    return FixedPrices()


@pytest.fixture
def ctx(prices: FixedPrices) -> EngineContext:
    return engine_ctx(prices)


# --------------------------------------------------------------------------------- seeded personas + HTTP client
PERSONA_IDS = [p.user_id for p in PERSONAS.values()]


@pytest_asyncio.fixture(scope="module")
async def seed_now(test_database: str) -> AsyncIterator[datetime]:
    """Seed the universe + Priya/Alex/Sam once per MODULE at the real `now` (dev JWTs and the FakeClock agree).
    Module scope plus a full teardown keeps the seeded universe from leaking into the suites that run after
    ours (they assume an empty `tickers` / `prices_daily`)."""
    now = datetime.now(UTC)
    conn = await asyncpg.connect(test_database)
    await init_connection(conn)
    try:
        await seed(conn, now=now, reset=True)
        yield now
        await conn.execute("delete from users where id = any($1::uuid[])", PERSONA_IDS)
        await conn.execute("delete from prices_daily")
        await conn.execute("delete from tickers")
    finally:
        await conn.close()


@pytest.fixture
def reseed(test_database: str, seed_now: datetime) -> Callable[..., Awaitable[None]]:
    async def _run(*keys: str) -> None:
        conn = await asyncpg.connect(test_database)
        await init_connection(conn)
        try:
            await seed(conn, now=seed_now, reset=True, personas=list(keys) or None)
        finally:
            await conn.close()

    return _run


@pytest_asyncio.fixture
async def dclient(
    app_factory: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]], seed_now: datetime
) -> AsyncIterator[httpx.AsyncClient]:
    """App with a FakeClock at the seed time, market OPEN and the DB last-close price source."""
    clock = FakeClock(start=seed_now)
    async with app_factory(clock=clock, rate_limit_enabled=False) as client:
        app = client.app  # type: ignore[attr-defined]
        app.state.market_override = True
        app.state.price_source = DbPriceSource(get_db().pool, app.state, clock.now)
        client.clock = clock  # type: ignore[attr-defined]
        yield client


async def login(client: httpx.AsyncClient, persona: str) -> tuple[dict[str, str], UUID]:
    r = await client.post("/v1/auth/dev-login", json={"persona": persona})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, UUID(r.json()["user"]["user_id"])
