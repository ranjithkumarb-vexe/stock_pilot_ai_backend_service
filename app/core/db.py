"""asyncpg pools and transaction helpers.

* The API pool connects as role `stockpilot_app` (NOSUPERUSER, NOBYPASSRLS): Row-Level Security is enforced.
* `user_tx(user_id)` opens a transaction and sets `app.user_id` (transaction-local) so every policy
  `user_id = nullif(current_setting('app.user_id', true), '')::uuid` resolves to that user.
* `system_tx()` uses the owner/admin URL (BYPASSRLS). Jobs, migrations and seeding only — never from a
  request handler acting for a user.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import orjson
from asyncpg import Connection, Pool

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger("app.db")

__all__ = [
    "Connection",
    "Database",
    "Pool",
    "as_uuid",
    "get_db",
    "init_connection",
    "set_database",
    "system_tx",
    "user_tx",
    "vec_literal",
]


async def init_connection(conn: Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(
            typ,
            encoder=lambda v: orjson.dumps(v).decode(),
            decoder=orjson.loads,
            schema="pg_catalog",
        )


def vec_literal(values: Sequence[float]) -> str:
    """pgvector text literal; pass as a parameter and cast in SQL: `$1::vector`."""
    return "[" + ",".join(f"{float(v):.8g}" for v in values) + "]"


def as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: Pool | None = None
        self._admin_pool: Pool | None = None
        self._admin_lock = asyncio.Lock()
        self.degraded = False

    async def open(self) -> None:
        s = self._settings
        kwargs: dict[str, Any] = {
            "max_size": s.db_pool_max,
            "init": init_connection,
            "command_timeout": s.request_timeout_s,
            "server_settings": {
                "application_name": "stockpilot-api",
                "statement_timeout": str(s.db_statement_timeout_ms),
                "idle_in_transaction_session_timeout": "60000",
            },
        }
        try:
            self._pool = await asyncpg.create_pool(s.database_url, min_size=s.db_pool_min, **kwargs)
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            # keep serving /healthz and a truthful 503 on /readyz instead of crashing at boot
            log.error("database unavailable at startup", extra={"error": type(exc).__name__})
            self.degraded = True
            self._pool = await asyncpg.create_pool(s.database_url, min_size=0, **kwargs)

    async def close(self) -> None:
        for pool in (self._pool, self._admin_pool):
            if pool is not None:
                await pool.close()
        self._pool = None
        self._admin_pool = None

    @property
    def pool(self) -> Pool:
        if self._pool is None:
            raise RuntimeError("database not opened")
        return self._pool

    async def _get_admin_pool(self) -> Pool:
        async with self._admin_lock:
            if self._admin_pool is None:
                s = self._settings
                self._admin_pool = await asyncpg.create_pool(
                    s.database_admin_url,
                    min_size=0,
                    max_size=s.db_admin_pool_max,
                    init=init_connection,
                    command_timeout=300,
                    server_settings={"application_name": "stockpilot-system"},
                )
            return self._admin_pool

    async def ping(self, timeout: float = 2.0) -> bool:
        try:
            async with asyncio.timeout(timeout), self.pool.acquire() as conn:
                return bool((await conn.fetchval("select 1")) == 1)
        except (OSError, asyncpg.PostgresError, TimeoutError, RuntimeError):
            return False

    @asynccontextmanager
    async def user_tx(self, user_id: UUID | str) -> AsyncIterator[Connection]:
        uid = str(as_uuid(user_id))
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.user_id', $1, true)", uid)
            yield conn

    @asynccontextmanager
    async def system_tx(self) -> AsyncIterator[Connection]:
        pool = await self._get_admin_pool()
        async with pool.acquire() as conn, conn.transaction():
            yield conn


_db: Database | None = None


def set_database(db: Database | None) -> None:
    global _db
    _db = db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("database not initialised (app lifespan not running?)")
    return _db


@asynccontextmanager
async def user_tx(user_id: UUID | str) -> AsyncIterator[Connection]:
    """Transaction as the given user: RLS applies. Commits on success, rolls back on error."""
    async with get_db().user_tx(user_id) as conn:
        yield conn


@asynccontextmanager
async def system_tx() -> AsyncIterator[Connection]:
    """Transaction as the owner (RLS bypassed). Jobs/migrations/seed only."""
    async with get_db().system_tx() as conn:
        yield conn
