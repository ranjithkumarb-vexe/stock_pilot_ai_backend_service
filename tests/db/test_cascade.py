"""Deleting a user removes every user-owned row (incl. embeddings); other tenants are untouched."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.db.rls_specs import RLS_TABLES, TABLE_SPECS, create_prereq

Conn = Any


async def test_delete_user_cascades_through_every_user_table(
    db_admin: Conn, db_as_user: Callable[..., Any]
) -> None:
    gone, kept = await create_prereq(db_admin), await create_prereq(db_admin)
    for owner in (gone, kept):
        async with db_as_user(owner.user_id) as conn:
            for spec in TABLE_SPECS.values():
                if spec.insert is not None:
                    await spec.insert(conn, owner)

    async def counts(uid: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in RLS_TABLES:
            col = TABLE_SPECS[table].owner_col
            out[table] = int(await db_admin.fetchval(f"select count(*) from {table} where {col} = $1", uid))
        return out

    before_gone, before_kept = await counts(gone.user_id), await counts(kept.user_id)
    assert all(n >= 1 for n in before_gone.values()), before_gone  # every table really had data
    assert (
        await db_admin.fetchval(
            "select count(*) from memories where user_id = $1 and embedding is not null",
            gone.user_id,
        )
        == 1
    )

    await db_admin.execute("delete from users where id = $1", gone.user_id)

    after = await counts(gone.user_id)
    # audit_log has no FK on purpose (anonymised by anonymize_audit_log instead, see test_audit_log.py)
    assert {t: n for t, n in after.items() if n and t != "audit_log"} == {}
    assert await counts(kept.user_id) == before_kept
    await db_admin.execute("delete from users where id = $1", kept.user_id)
