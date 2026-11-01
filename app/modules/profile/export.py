"""JSON export of everything stored for one user (FR-09). Read under RLS, so it can only ever see own rows.

Tables are discovered from the catalog (every public table with a `user_id` column, plus `users`), so a table
added later is exported automatically. Embeddings are dropped (large, not user-readable); stream-token hashes
are secrets and excluded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import orjson

from app.core.db import Connection

EXCLUDE_TABLES = frozenset({"stream_tokens"})
DROP_COLUMNS = ("embedding",)
ROW_LIMIT = 50_000


async def export_tables(conn: Connection) -> list[str]:
    rows = await conn.fetch(
        """
        select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r'
           and exists (select 1 from pg_attribute a where a.attrelid = c.oid and a.attname = 'user_id' and not a.attisdropped)
         order by c.relname
        """
    )
    return [r["relname"] for r in rows if r["relname"] not in EXCLUDE_TABLES]


async def export_user_data(conn: Connection, user_id: UUID, now: datetime) -> dict[str, Any]:
    """`{exported_at, user, tables: {name: [row, ...]}}`; JSON-serialisable with `orjson`."""
    tables: dict[str, Any] = {}
    truncated: list[str] = []
    for name in await export_tables(conn):
        drop = " ".join(f"- '{c}'" for c in DROP_COLUMNS)
        rows = await conn.fetch(f'select (to_jsonb(t) {drop}) as j from "{name}" t limit {ROW_LIMIT + 1}')  # noqa: S608 - names come from the catalog
        data = [r["j"] for r in rows[:ROW_LIMIT]]
        if len(rows) > ROW_LIMIT:
            truncated.append(name)
        tables[name] = data
    user = await conn.fetchrow("select to_jsonb(u) as j from users u where id = $1", user_id)
    return {
        "exported_at": now.isoformat().replace("+00:00", "Z"),
        "format": "stockpilot-export/1",
        "user": user["j"] if user else None,
        "tables": tables,
        "truncated": truncated,
    }


def dumps(data: dict[str, Any]) -> bytes:
    return orjson.dumps(data, option=orjson.OPT_INDENT_2)
