"""audit_log is append-only; account deletion keeps rows but anonymises them (SECURITY DEFINER fn)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import asyncpg
import pytest

Conn = Any


async def _insert(conn: Conn, uid: UUID, action: str = "t.x", details: dict[str, Any] | None = None) -> int:
    return int(
        await conn.fetchval(
            "insert into audit_log (user_id, action, entity, details) values ($1, $2, 'e', $3) returning id",
            uid,
            action,
            details or {},
        )
    )


async def test_owner_cannot_update_delete_or_truncate(db_admin: Conn, make_user: Callable[..., Any]) -> None:
    uid = await make_user()
    row_id = await _insert(db_admin, uid)
    for stmt in (
        f"update audit_log set action = 'tampered' where id = {row_id}",
        f"delete from audit_log where id = {row_id}",
        "truncate audit_log",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await db_admin.execute(stmt)
    assert await db_admin.fetchval("select action from audit_log where id = $1", row_id) == "t.x"


async def test_owner_cannot_anonymise_by_hand_with_other_changes(
    db_admin: Conn, make_user: Callable[..., Any]
) -> None:
    uid = await make_user()
    row_id = await _insert(db_admin, uid)
    async with db_admin.transaction():
        await db_admin.execute("select set_config('sp.audit_anonymize', 'on', true)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            # flag alone is not enough: only user_id -> NULL is permitted, never other columns
            await db_admin.execute(
                "update audit_log set action = 'forged', user_id = null where id = $1", row_id
            )


async def test_app_role_can_read_and_append_only_own_rows(
    db_as_user: Callable[..., Any], make_user: Callable[..., Any]
) -> None:
    a, b = await make_user(), await make_user()
    async with db_as_user(a) as conn:
        row_id = await _insert(conn, a)
        assert await conn.fetchval("select count(*) from audit_log where user_id = $1", a) >= 1
        for stmt in (
            f"update audit_log set action = 'x' where id = {row_id}",
            f"delete from audit_log where id = {row_id}",
            "truncate audit_log",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(stmt)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # cannot write another user's audit row
            async with conn.transaction():
                await _insert(conn, b)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # forging the flag does not help the app role
            async with conn.transaction():
                await conn.execute("select set_config('sp.audit_anonymize', 'on', true)")
                await conn.execute("update audit_log set user_id = null where id = $1", row_id)


async def test_anonymize_only_after_account_deleted_and_only_for_self(
    db_admin: Conn, db_as_user: Callable[..., Any], make_user: Callable[..., Any]
) -> None:
    a, b = await make_user(), await make_user()
    async with db_as_user(a) as conn:
        await _insert(conn, a, "profile.update", {"email": "a@x", "user_hash": "abc123"})
        await _insert(conn, a, "order.create", {"qty": 5})
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="still exists"):
            async with conn.transaction():
                await conn.fetchval("select anonymize_audit_log($1)", a)
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="context mismatch"):
            async with conn.transaction():
                await conn.fetchval("select anonymize_audit_log($1)", b)

    async with db_as_user(a) as conn:
        await conn.execute("delete from users where id = $1", a)  # cascade removes user data
        n = await conn.fetchval("select anonymize_audit_log($1)", a)
        assert n >= 2

    rows = await db_admin.fetch(
        "select user_id, action, details from audit_log where action in ('profile.update', 'order.create') "
        "and details ->> 'anonymised' = 'true' order by id desc limit 2"
    )
    assert len(rows) == 2
    assert all(r["user_id"] is None for r in rows)
    kept = {r["action"]: r["details"] for r in rows}
    assert kept["profile.update"] == {"anonymised": True, "user_hash": "abc123"}  # email scrubbed
    assert kept["order.create"] == {"anonymised": True}
    assert await db_admin.fetchval("select count(*) from audit_log where user_id = $1", a) == 0
