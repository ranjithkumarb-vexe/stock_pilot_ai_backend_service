"""BE-02: migrations apply on an empty DB, are idempotent via the runner, and are tamper-evident."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import asyncpg
import pytest

from app.core.config import Settings, dsn_with_database
from app.core.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    ensure_database,
    load_migrations,
    migrate,
    reset_schema,
)


@pytest.fixture
async def scratch_dsn(settings: Settings) -> str:
    """An empty scratch database (name derived from SP_TEST_DB so parallel agents do not collide)."""
    name = f"{settings.sp_test_db}_mig"
    admin = dsn_with_database(settings.database_admin_url, name)
    await ensure_database(settings.database_admin_url, name)
    conn = await asyncpg.connect(admin)
    try:
        await reset_schema(conn)
    finally:
        await conn.close()
    return admin


async def test_foundation_migrations_exist_and_are_ordered() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
    assert versions[:8] == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_apply_on_empty_db_then_idempotent(scratch_dsn: str) -> None:
    expected = [m.version for m in load_migrations()]
    first = await migrate(scratch_dsn)
    assert first == expected
    second = await migrate(scratch_dsn)
    assert second == []  # idempotent
    conn = await asyncpg.connect(scratch_dsn)
    try:
        rows = await conn.fetch("select version, name, checksum from schema_migrations order by version")
        assert [r["version"] for r in rows] == expected
        exts = {r["extname"] for r in await conn.fetch("select extname from pg_extension")}
        assert {"vector", "pgcrypto", "uuid-ossp", "pg_trgm"} <= exts
        role = await conn.fetchrow(
            "select rolsuper, rolbypassrls from pg_roles where rolname = 'stockpilot_app'"
        )
        assert not role["rolsuper"] and not role["rolbypassrls"]
        # required indexes (SPEC §8)
        idx = {
            r["indexdef"]
            for r in await conn.fetch("select indexdef from pg_indexes where schemaname = 'public'")
        }
        assert any("USING hnsw (embedding" in d and "doc_chunks" in d for d in idx)
        assert any("USING gin (tsv)" in d and "doc_chunks" in d for d in idx)
        assert any("events" in d and "(user_id, ts" in d for d in idx)
        assert any("alerts" in d and "(user_id, created_at" in d for d in idx)
        assert any("orders" in d and "(user_id, status)" in d for d in idx)
        dim = await conn.fetchval(
            "select atttypmod from pg_attribute where attrelid = 'doc_chunks'::regclass and attname = 'embedding'"
        )
        assert dim == 1024
    finally:
        await conn.close()


async def test_concurrent_runners_are_serialised(scratch_dsn: str) -> None:
    results = await asyncio.gather(migrate(scratch_dsn), migrate(scratch_dsn))
    applied = sorted(v for r in results for v in r)
    assert applied == [m.version for m in load_migrations()]  # each migration applied exactly once


async def test_modified_applied_migration_is_rejected(scratch_dsn: str, tmp_path: Path) -> None:
    work = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, work)
    await migrate(scratch_dsn, directory=work)
    (work / "0002_reference_data.sql").write_text(
        (work / "0002_reference_data.sql").read_text() + "\n-- sneaky edit\n", encoding="utf-8"
    )
    with pytest.raises(MigrationError, match="changed after being applied"):
        await migrate(scratch_dsn, directory=work)


async def test_new_migration_file_applies_alone_and_bad_names_fail(scratch_dsn: str, tmp_path: Path) -> None:
    work = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, work)
    await migrate(scratch_dsn, directory=work)
    (work / "0100_demo.sql").write_text("create table demo_a (id int primary key);", encoding="utf-8")
    assert await migrate(scratch_dsn, directory=work) == [100]
    (work / "oops.sql").write_text("select 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="bad migration file name"):
        load_migrations(work)
    (work / "oops.sql").unlink()
    (work / "0100_other.sql").write_text("select 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate migration version"):
        load_migrations(work)


async def test_failed_migration_rolls_back_and_is_not_recorded(scratch_dsn: str, tmp_path: Path) -> None:
    work = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, work)
    await migrate(scratch_dsn, directory=work)
    (work / "0101_broken.sql").write_text("create table half_done (id int);\nselect 1/0;", encoding="utf-8")
    with pytest.raises(asyncpg.DataError):
        await migrate(scratch_dsn, directory=work)
    conn = await asyncpg.connect(scratch_dsn)
    try:
        assert await conn.fetchval("select to_regclass('public.half_done')") is None
        assert await conn.fetchval("select count(*) from schema_migrations where version = 101") == 0
    finally:
        await conn.close()


async def test_runner_refuses_non_owner_role(scratch_dsn: str, settings: Settings) -> None:
    """0001 fails fast when run by a role that cannot bypass RLS (SECURITY DEFINER functions rely on it)."""
    await migrate(scratch_dsn)
    conn = await asyncpg.connect(dsn_with_database(settings.database_url, f"{settings.sp_test_db}_mig"))
    try:
        sql = (MIGRATIONS_DIR / "0001_extensions_roles.sql").read_text(encoding="utf-8")
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(sql)
    finally:
        await conn.close()


async def test_apply_migrations_function_on_prepared_connection(scratch_dsn: str) -> None:
    conn = await asyncpg.connect(scratch_dsn)
    try:
        assert await apply_migrations(conn) != []
        assert await apply_migrations(conn) == []
    finally:
        await conn.close()
