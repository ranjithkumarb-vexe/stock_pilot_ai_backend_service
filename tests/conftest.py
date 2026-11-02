"""Shared pytest harness. Other agents rely on these fixture names (see docs/workflow/HANDOFF.md):

* ``db_admin``        - asyncpg connection as the owner/superuser to the freshly migrated per-session test DB
* ``db_as_user(uid)`` - async context manager -> connection in a transaction as role ``stockpilot_app``
                        with ``app.user_id`` set (this is what RLS tests use); commits on clean exit
* ``app_client``      - httpx.AsyncClient over the real ASGI app (lifespan running) against the test DB
* ``make_user(email)``- creates a ``users`` row, returns its UUID
* ``auth_headers(uid)``- dev-signed ``Authorization: Bearer`` headers
* ``fake_clock``      - deterministic :class:`FakeClock`
* ``settings``        - Settings pointing at the test DB;  ``app_factory`` - build apps with overrides
* ``ensure_tickers``  - insert reference tickers (FKs from orders/watchlist/... need them)

The test database is named by env ``SP_TEST_DB`` (default ``stockpilot_test``); it is created if missing
and its ``public`` schema is dropped and re-migrated at session start.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.core.auth import issue_dev_token
from app.core.clock import Clock, FakeClock
from app.core.config import Settings, dsn_database, dsn_with_database
from app.core.db import init_connection
from app.core.migrate import apply_migrations, ensure_database, reset_schema, set_app_role_password
from app.main import create_app

Conn = Any


@pytest.fixture(scope="session")
def settings() -> Settings:
    base = Settings(auth_mode="dev")  # explicit: AUTH_MODE has no default (SEC-03)
    name = base.sp_test_db
    if not re.search(r"(test|e2e)", name):
        raise RuntimeError(f"refusing to run tests against database {name!r}: SP_TEST_DB must contain 'test'")
    return base.model_copy(
        update={
            "database_url": dsn_with_database(base.database_url, name),
            "database_admin_url": dsn_with_database(base.database_admin_url, name),
            "app_env": "test",
            "auth_mode": "dev",
            "cors_origins": [],
        }
    )


@pytest_asyncio.fixture(scope="session")
async def test_database(settings: Settings) -> str:
    """Create (if missing), wipe and migrate the test database once per session."""
    admin_dsn = settings.database_admin_url
    await ensure_database(admin_dsn, dsn_database(admin_dsn))
    conn = await asyncpg.connect(admin_dsn)
    try:
        await reset_schema(conn)
        await apply_migrations(conn)
        await set_app_role_password(conn, settings.app_db_password)
    finally:
        await conn.close()
    return admin_dsn


@pytest_asyncio.fixture
async def db_admin(test_database: str) -> AsyncIterator[Conn]:
    conn = await asyncpg.connect(test_database)
    await init_connection(conn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def db_as_user(test_database: str, settings: Settings) -> Callable[[UUID | str | None], Any]:
    """``async with db_as_user(uid) as conn:`` - app-role connection, RLS enforced, one transaction.

    ``None`` means "no user context" (``app.user_id`` empty): nothing must be visible or writable.
    """

    @asynccontextmanager
    async def _cm(user_id: UUID | str | None) -> AsyncIterator[Conn]:
        conn = await asyncpg.connect(settings.database_url)
        await init_connection(conn)
        try:
            async with conn.transaction():
                await conn.execute("select set_config('app.user_id', $1, true)", str(user_id or ""))
                yield conn
        finally:
            await conn.close()

    return _cm


@pytest_asyncio.fixture
async def make_user(test_database: str) -> AsyncIterator[Callable[..., Awaitable[UUID]]]:
    created: list[UUID] = []
    conn = await asyncpg.connect(test_database)

    async def _make(email: str | None = None, display_name: str | None = None) -> UUID:
        uid = uuid4()
        await conn.execute(
            "insert into users (id, email, display_name) values ($1, $2, $3)",
            uid,
            email or f"u-{uid.hex[:10]}@example.test",
            display_name,
        )
        created.append(uid)
        return uid

    try:
        yield _make
    finally:
        if created:
            await conn.execute("delete from users where id = any($1::uuid[])", created)
        await conn.close()


@pytest.fixture
def ensure_tickers(db_admin: Conn) -> Callable[[Iterable[str]], Awaitable[None]]:
    async def _ensure(symbols: Iterable[str]) -> None:
        for sym in symbols:
            await db_admin.execute(
                "insert into tickers (symbol, name, sector, in_universe) values ($1, $1, 'Test', true) "
                "on conflict do nothing",
                sym,
            )

    return _ensure


@pytest.fixture
def auth_headers(settings: Settings) -> Callable[..., dict[str, str]]:
    def _headers(user_id: UUID | str, email: str | None = None, ttl_s: int | None = None) -> dict[str, str]:
        token = issue_dev_token(settings, UUID(str(user_id)), email, now=datetime.now(UTC), ttl_s=ttl_s)
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app_factory(test_database: str, settings: Settings) -> Callable[..., Any]:
    """``async with app_factory(rate_limit_ip_burst=3, clock=...) as client:`` - custom settings/clock."""

    @asynccontextmanager
    async def _make(clock: Clock | None = None, **overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(settings.model_copy(update=overrides), clock=clock)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4242))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.app = app  # type: ignore[attr-defined]
                yield client

    return _make


@pytest_asyncio.fixture
async def app_client(app_factory: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with app_factory() as client:
        yield client
