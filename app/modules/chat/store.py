"""Chat threads/messages (user tables, RLS). Raw chat text is kept 30 days (SPEC 11.3)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.cursors import parse_timestamp_cursor
from app.core.db import Connection
from app.core.errors import not_found
from app.modules.chat.schemas import HistoryItem

RETENTION_DAYS = 30


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


async def ensure_thread(conn: Connection, user_id: UUID, thread_id: UUID | None, title: str | None) -> UUID:
    if thread_id is None:
        return UUID(str(await conn.fetchval("insert into chat_threads (user_id, title) values ($1, $2) returning id", user_id, title)))  # fmt: skip
    if (
        await conn.fetchval("select 1 from chat_threads where id = $1 and user_id = $2", thread_id, user_id)
        is None
    ):
        raise not_found("Thread not found")
    return thread_id


async def add_message(
    conn: Connection,
    user_id: UUID,
    thread_id: UUID,
    *,
    role: str,
    content: str,
    citations: list[dict[str, Any]] | None = None,
    refused: bool = False,
    refusal_reason: str | None = None,
    trace_id: str | None = None,
    intent: str | None = None,
    ledger: dict[str, Any] | None = None,
) -> tuple[UUID, datetime]:
    row = await conn.fetchrow(
        "insert into chat_messages (thread_id, user_id, role, content, citations, refused, refusal_reason, trace_id, "
        "intent, ledger, created_at) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, clock_timestamp()) returning id, created_at",
        thread_id, user_id, role, content, citations or [], refused, refusal_reason, trace_id, intent, ledger or {},
    )  # fmt: skip
    assert row is not None
    await conn.execute(
        "update chat_threads set updated_at = now() where id = $1 and user_id = $2", thread_id, user_id
    )
    return UUID(str(row["id"])), row["created_at"]


async def purge_expired(conn: Connection, user_id: UUID, now: datetime) -> int:
    """Delete this user's raw chat text older than the retention window; drop emptied threads."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    res = await conn.execute(
        "delete from chat_messages where user_id = $1 and created_at < $2", user_id, cutoff
    )
    await conn.execute(
        "delete from chat_threads t where t.user_id = $1 and not exists (select 1 from chat_messages m where m.thread_id = t.id)"
        " and t.updated_at < $2",
        user_id, cutoff,
    )  # fmt: skip
    return int(res.split()[-1]) if res else 0


async def history(
    conn: Connection, user_id: UUID, thread_id: UUID | None, limit: int, cursor: str | None
) -> tuple[list[HistoryItem], str | None]:
    """Newest `limit` messages of a thread (default: the most recent thread), oldest first.

    `cursor` = created_at of the oldest message already seen (ISO); the next page is the messages before it.
    """
    before = (
        parse_timestamp_cursor(cursor) if cursor else None
    )  # validate first: a bad cursor is a 422 even for an empty history
    if thread_id is None:
        thread_id = await conn.fetchval(
            "select id from chat_threads where user_id = $1 order by updated_at desc, created_at desc limit 1",
            user_id,
        )
        if thread_id is None:
            return [], None
    elif (
        await conn.fetchval("select 1 from chat_threads where id = $1 and user_id = $2", thread_id, user_id)
        is None
    ):
        raise not_found("Thread not found")
    rows = await conn.fetch(
        "select id, thread_id, role, content, created_at, refused, refusal_reason, citations, trace_id "
        "from chat_messages where thread_id = $1 and user_id = $2 and ($3::timestamptz is null or created_at < $3) "
        "order by created_at desc, id desc limit $4",
        thread_id, user_id, before, limit + 1,
    )  # fmt: skip
    more = len(rows) > limit
    rows = list(rows[:limit])
    rows.reverse()
    items = [
        HistoryItem(
            id=str(r["id"]), thread_id=str(r["thread_id"]), role=r["role"], text=r["content"],
            created_at=iso(r["created_at"]), refused=r["refused"], refusal_reason=r["refusal_reason"],
            citations=list(r["citations"] or []), trace_id=r["trace_id"],
        )
        for r in rows
    ]  # fmt: skip
    return items, (items[0].created_at if more and items else None)
