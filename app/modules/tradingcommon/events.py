"""Server-side behaviour events (SPEC 10.2) written inside the caller's user transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.core.db import Connection


async def emit_event(
    conn: Connection,
    *,
    user_id: UUID,
    ts: datetime,
    type: str,
    entity: dict[str, Any] | None = None,
    props: dict[str, Any] | None = None,
    session_id: str = "server",
) -> UUID:
    """Insert one row into `events` (RLS: the acting user's). Returns the event id.

    Props must not carry free text from the user; ids, symbols, numbers and enum values only.
    """
    event_id = uuid4()
    await conn.execute(
        """
        insert into events (event_id, user_id, ts, session_id, type, entity, props, context, client, schema_version)
        values ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb, '{"device": "server", "tz": "UTC"}'::jsonb, 1)
        """,
        event_id,
        user_id,
        ts,
        session_id,
        type,
        entity or {},
        props or {},
    )
    return event_id


async def universe_row(conn: Connection, symbol: str) -> dict[str, Any] | None:
    """`tickers` row of a supported (in_universe) symbol, else None."""
    row = await conn.fetchrow(
        "select symbol, name, sector, industry, exchange from tickers where symbol = $1 and in_universe",
        symbol,
    )
    return dict(row) if row else None
