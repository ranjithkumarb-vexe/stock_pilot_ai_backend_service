"""Forward-only SQL migration runner.

* Files: `migrations/NNNN_name.sql` (blocks reserved per stream, see TASKS.md). Applied in numeric order.
* Each file runs in its own transaction together with its `schema_migrations` row.
* Idempotent: applied versions are skipped; an applied file whose checksum changed is an error.
* A session-level advisory lock serialises concurrent runners.
* CLI: `python -m app.core.migrate` (uses DATABASE_ADMIN_URL). Then, if APP_DB_PASSWORD is set, the
  password of role `stockpilot_app` is (re)applied — deployments override the dev default this way.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.core.config import APP_ROLE_NAME, Settings, dsn_with_database, get_settings
from app.core.logging import get_logger

log = get_logger("app.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x5350_4D49  # "SPMI"

_BOOTSTRAP_SQL = """
create table if not exists schema_migrations (
    version    integer primary key,
    name       text not null,
    checksum   text not null,
    applied_at timestamptz not null default now()
)
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    seen: dict[int, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_RE.match(path.name)
        if match is None:
            raise MigrationError(f"bad migration file name (want NNNN_name.sql): {path.name}")
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(f"duplicate migration version {version}: {seen[version].name}, {path.name}")
        seen[version] = path
        found.append(Migration(version, match.group(2), path, path.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    return found


async def apply_migrations(
    conn: asyncpg.Connection,
    directory: Path = MIGRATIONS_DIR,
) -> list[int]:
    """Apply all pending migrations on `conn` (an owner/admin connection). Returns versions applied."""
    migrations = load_migrations(directory)
    await conn.execute("select pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await conn.execute(_BOOTSTRAP_SQL)
        applied = {
            r["version"]: r["checksum"]
            for r in await conn.fetch("select version, checksum from schema_migrations")
        }
        newly: list[int] = []
        for mig in migrations:
            if mig.version in applied:
                if applied[mig.version] != mig.checksum:
                    raise MigrationError(
                        f"migration {mig.path.name} changed after being applied (forward-only: add a new file)"
                    )
                continue
            async with conn.transaction():
                await conn.execute(mig.sql)
                await conn.execute(
                    "insert into schema_migrations (version, name, checksum) values ($1, $2, $3)",
                    mig.version,
                    mig.name,
                    mig.checksum,
                )
            log.info("migration applied", extra={"version": mig.version, "migration": mig.name})
            newly.append(mig.version)
        return newly
    finally:
        await conn.execute("select pg_advisory_unlock($1)", _LOCK_KEY)


async def set_app_role_password(conn: asyncpg.Connection, password: str) -> None:
    stmt = await conn.fetchval(
        "select format('alter role %I password %L', $1::text, $2::text)", APP_ROLE_NAME, password
    )
    await conn.execute(stmt)


async def ensure_database(admin_dsn: str, dbname: str) -> bool:
    """Create database `dbname` if missing (connects via the `postgres` maintenance db). True if created."""
    dsn_with_database(admin_dsn, dbname)  # validates the identifier
    conn = await asyncpg.connect(dsn_with_database(admin_dsn, "postgres"))
    try:
        exists = await conn.fetchval("select 1 from pg_database where datname = $1", dbname)
        if exists:
            return False
        await conn.execute(f'create database "{dbname}"')
        return True
    finally:
        await conn.close()


async def reset_schema(conn: asyncpg.Connection) -> None:
    """Drop everything in `public` (tables, extensions' objects, functions) and recreate it empty.
    Test databases only."""
    for ext in ("vector", "pg_trgm", "uuid-ossp", "pgcrypto"):
        await conn.execute(f'drop extension if exists "{ext}" cascade')
    await conn.execute("drop schema if exists public cascade")
    await conn.execute("create schema public")


async def migrate(
    dsn: str, *, app_password: str | None = None, directory: Path = MIGRATIONS_DIR
) -> list[int]:
    conn = await asyncpg.connect(dsn)
    try:
        applied = await apply_migrations(conn, directory)
        if app_password:
            await set_app_role_password(conn, app_password)
        return applied
    finally:
        await conn.close()


def main(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    applied = asyncio.run(migrate(settings.database_admin_url, app_password=settings.app_db_password))
    print(f"migrations applied: {applied if applied else 'none (up to date)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
