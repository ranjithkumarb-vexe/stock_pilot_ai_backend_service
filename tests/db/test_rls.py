"""NFR-03 / T-SEC-03: tenant isolation on every user table, proven by probing as the app role."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from tests.db.rls_specs import (
    REFERENCE_TABLES,
    RLS_TABLES,
    SYSTEM_TABLES,
    TABLE_SPECS,
    probe_table,
)

Conn = Any


async def _public_tables(admin: Conn) -> set[str]:
    rows = await admin.fetch(
        "select table_name from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'"
    )
    return {r["table_name"] for r in rows}


async def test_rls_table_list_equals_every_table_with_user_id(db_admin: Conn) -> None:
    """A new table with a `user_id` column that is not in the list (=> not probed) fails this test."""
    rows = await db_admin.fetch(
        "select c.table_name from information_schema.columns c join information_schema.tables t "
        "on t.table_schema = c.table_schema and t.table_name = c.table_name "
        "where c.table_schema = 'public' and c.column_name = 'user_id' and t.table_type = 'BASE TABLE'"
    )
    with_user_id = {r["table_name"] for r in rows}
    assert set(RLS_TABLES) == with_user_id | {"users"}, (
        f"untested user tables: {sorted((with_user_id | {'users'}) - set(RLS_TABLES))}; "
        f"stale entries: {sorted(set(RLS_TABLES) - with_user_id - {'users'})}"
    )


async def test_every_public_table_is_classified(db_admin: Conn) -> None:
    tables = await _public_tables(db_admin)
    classified = set(RLS_TABLES) | REFERENCE_TABLES | SYSTEM_TABLES
    assert tables == classified, (
        f"unclassified: {sorted(tables - classified)}, missing: {sorted(classified - tables)}"
    )
    assert set(TABLE_SPECS) == set(RLS_TABLES)


@pytest.mark.parametrize("table", RLS_TABLES)
async def test_rls_enabled_forced_with_tenant_policy(db_admin: Conn, table: str) -> None:
    row = await db_admin.fetchrow(
        "select c.relrowsecurity, c.relforcerowsecurity from pg_class c "
        "where c.oid = ('public.' || quote_ident($1))::regclass",
        table,
    )
    assert row["relrowsecurity"] and row["relforcerowsecurity"], f"{table}: RLS must be enabled and FORCED"
    pols = await db_admin.fetch(
        "select policyname, qual, with_check from pg_policies where schemaname = 'public' and tablename = $1",
        table,
    )
    assert pols, f"{table}: no policy"
    for pol in pols:
        assert "app.user_id" in pol["qual"], f"{table}.{pol['policyname']}: policy must key on app.user_id"
        assert "app.user_id" in pol["with_check"], (
            f"{table}.{pol['policyname']}: WITH CHECK must key on app.user_id"
        )


@pytest.mark.parametrize("table", RLS_TABLES)
async def test_cross_tenant_isolation(db_admin: Conn, db_as_user: Callable[..., Any], table: str) -> None:
    """User B cannot SELECT / INSERT / UPDATE / DELETE user A's rows (and A's own access still works)."""
    failures = await probe_table(table, db_admin, db_as_user)
    assert failures == [], f"{table}: {failures}"


async def test_app_role_is_not_privileged(db_admin: Conn) -> None:
    row = await db_admin.fetchrow(
        "select rolsuper, rolbypassrls, rolcreatedb, rolcreaterole from pg_roles where rolname = 'stockpilot_app'"
    )
    assert not any(row.values())


@pytest.mark.parametrize("table", sorted(REFERENCE_TABLES))
async def test_reference_tables_read_only_for_app_role(
    db_admin: Conn, db_as_user: Callable[..., Any], table: str, make_user: Callable[..., Any]
) -> None:
    uid: UUID = await make_user()
    first_col = await db_admin.fetchval(
        "select column_name from information_schema.columns where table_schema = 'public' "
        "and table_name = $1 and is_identity = 'NO' and is_generated = 'NEVER' order by ordinal_position limit 1",
        table,
    )
    async with db_as_user(uid) as conn:
        await conn.fetchval(f"select count(*) from {table}")
        for stmt in (
            f"delete from {table}",
            f"update {table} set {first_col} = {first_col}",
            f"truncate {table}",
            f"insert into {table} default values",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(stmt)


# ---------------------------------------------------------------------------------------------
# Mutation check: weaken ONE table's protection inside a scratch transaction and prove the probe
# notices. Rolled back afterwards, so the real schema is untouched.
# ---------------------------------------------------------------------------------------------
MUTATIONS = {
    "drop_policy": "drop policy tenant_isolation on {t}",
    "disable_rls": "alter table {t} disable row level security",
    "permissive_policy": (
        "drop policy tenant_isolation on {t}; "
        "create policy tenant_isolation on {t} for all using (true) with check (true)"
    ),
}


def _scratch_as_user(conn: Conn) -> Callable[[UUID | None], Any]:
    """Same semantics as `db_as_user`, on ONE connection inside the caller's scratch transaction."""

    @asynccontextmanager
    async def _cm(user_id: UUID | None) -> Any:
        async with conn.transaction():  # savepoint
            await conn.execute("set local role stockpilot_app")
            await conn.execute("select set_config('app.user_id', $1, true)", str(user_id or ""))
            try:
                yield conn
            finally:
                await conn.execute("reset role")

    return _cm


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
@pytest.mark.parametrize("table", RLS_TABLES)
async def test_mutation_check_isolation_test_fails_when_protection_is_removed(
    db_admin: Conn, table: str, mutation: str
) -> None:
    tx = db_admin.transaction()
    await tx.start()
    try:
        # sanity: the unmutated schema passes under the same scratch harness
        assert await probe_table(table, db_admin, _scratch_as_user(db_admin)) == []
        for stmt in MUTATIONS[mutation].format(t=f"public.{table}").split(";"):
            if stmt.strip():
                await db_admin.execute(stmt)
        failures = await probe_table(table, db_admin, _scratch_as_user(db_admin))
        assert failures, f"{table}: probe did NOT detect '{mutation}' - the isolation test is blind"
    finally:
        await tx.rollback()
    # and the real schema is intact
    row = await db_admin.fetchrow(
        "select relrowsecurity, (select count(*) from pg_policy where polrelid = c.oid) n "
        "from pg_class c where oid = ('public.' || quote_ident($1))::regclass",
        table,
    )
    assert row["relrowsecurity"] and row["n"] == 1
