"""Unit tests for core helpers (no database unless a fixture asks for one)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.clock import Clock, FakeClock, SystemClock
from app.core.config import Settings, dsn_database, dsn_with_database, dsn_with_user
from app.core.db import system_tx, user_tx, vec_literal
from app.core.pricesource import PriceSource, Quote, QuoteUnavailableError, StaticPriceSource
from app.core.ratelimit import TokenBucketLimiter
from app.core.schemas import Money, Pct, Price
from app.main import create_app, discover_modules
from tests.prod_config import prod_kwargs


async def test_fake_clock_advances_wall_and_monotonic_time() -> None:
    clock = FakeClock(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    m0 = clock.monotonic()
    clock.advance(90)
    assert clock.now() == datetime(2026, 1, 2, 3, 5, 35, tzinfo=UTC)
    assert clock.monotonic() == m0 + 90
    await clock.sleep(10)
    assert clock.now().second == 45
    clock.set(datetime(2026, 1, 3, tzinfo=UTC))
    assert clock.now().day == 3
    with pytest.raises(ValueError, match="timezone"):
        FakeClock(datetime(2026, 1, 1))
    assert isinstance(clock, Clock)
    sys_clock = SystemClock()
    assert sys_clock.now().tzinfo is not None
    assert isinstance(sys_clock, Clock)
    await sys_clock.sleep(0)


async def test_static_price_source_satisfies_protocol() -> None:
    ts = datetime(2026, 10, 30, 20, tzinfo=UTC)
    src = StaticPriceSource({"AAPL": Decimal("231.50")}, ts)
    assert isinstance(src, PriceSource)
    q = await src.get_last("AAPL")
    assert isinstance(q, Quote)
    assert q.price == Decimal("231.50")
    assert q.freshness == "live"
    with pytest.raises(QuoteUnavailableError):
        await src.get_last("ZZZZ")
    src.set_price("AAPL", Decimal("232"), ts + timedelta(seconds=5))
    assert [x.price async for x in src.subscribe(["AAPL"])] == [Decimal("232")]
    assert (await src.get_many(["AAPL"]))["AAPL"].price == Decimal("232")


def test_money_serialises_as_json_number() -> None:
    class Out(BaseModel):
        cash: Money
        px: Price
        w: Pct

    out = Out(cash=Decimal("22014.00"), px=Decimal("231.5000"), w=Decimal("9.4"))
    assert orjson.loads(out.model_dump_json()) == {"cash": 22014.0, "px": 231.5, "w": 9.4}
    assert out.cash == Decimal("22014.00")  # Decimal internally


def test_token_bucket_math_and_eviction() -> None:
    now = [0.0]
    lim = TokenBucketLimiter(burst=2, per_second=1.0, now=lambda: now[0])
    assert lim.check("k") == 0.0
    assert lim.check("k") == 0.0
    wait = lim.check("k")
    assert wait == pytest.approx(1.0)
    now[0] += 0.5
    assert lim.check("k") == pytest.approx(0.5)
    now[0] += 10
    assert lim.check("k") == 0.0
    assert lim.check("other") == 0.0  # keys are independent


def test_dsn_helpers() -> None:
    assert (
        dsn_with_database("postgresql://u:p@h:5432/a?host=/tmp", "b") == "postgresql://u:p@h:5432/b?host=/tmp"
    )
    assert dsn_database("postgresql://u@/stockpilot_dev?host=/tmp") == "stockpilot_dev"
    assert dsn_with_user("postgresql://u:p@h:5432/db", "x", "y") == "postgresql://x:y@h:5432/db"
    with pytest.raises(ValueError, match="unsafe"):
        dsn_with_database("postgresql://u@h/a", 'x"; drop database y; --')


def test_vec_literal() -> None:
    assert vec_literal([1, 0.5, -2]) == "[1,0.5,-2]"


def test_cors_origins_parse_from_env_string() -> None:
    s = Settings(_env_file=None, auth_mode="dev", cors_origins="http://a.test, http://b.test ,")
    assert s.cors_origins == ["http://a.test", "http://b.test"]
    assert Settings(_env_file=None, auth_mode="dev").docs_enabled is True


def test_module_auto_discovery_mounts_health_and_auth() -> None:
    names = [n for n, _ in discover_modules()]
    assert {"health", "auth"} <= set(names)
    app = create_app(Settings(_env_file=None, auth_mode="dev"))
    paths = set(app.openapi()["paths"])
    assert {"/healthz", "/readyz", "/v1/me", "/v1/auth/dev-login", "/v1/stream/token", "/v1/account"} <= paths


def test_prod_hides_docs() -> None:
    s = Settings(**prod_kwargs())
    app: FastAPI = create_app(s)
    assert app.openapi_url is None and app.docs_url is None


async def test_user_tx_sets_app_user_id_and_system_tx_bypasses_rls(
    app_factory: Callable[..., Any], make_user: Callable[..., Any]
) -> None:
    a, b = await make_user(), await make_user()
    async with app_factory():
        async with user_tx(a) as conn:
            assert str(await conn.fetchval("select current_setting('app.user_id')")) == str(a)
            assert await conn.fetchval("select count(*) from users where id = $1", b) == 0  # RLS hides b
            assert await conn.fetchval("select current_user") == "stockpilot_app"
        async with user_tx(a) as conn:  # transaction-local: nothing leaks to the next checkout
            await conn.execute("select 1")
        async with user_tx(b) as conn:
            assert str(await conn.fetchval("select current_setting('app.user_id')")) == str(b)
        async with system_tx() as conn:
            assert await conn.fetchval("select count(*) from users where id = any($1::uuid[])", [a, b]) == 2
        with pytest.raises(ValueError, match="badly formed"):
            async with user_tx("not-a-uuid"):
                pass
