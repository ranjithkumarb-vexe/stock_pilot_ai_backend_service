"""Small database helpers on the `alerts` table shared by the alert service and the ranker."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.db import Connection

SNOOZE = timedelta(hours=24)
LIVE_STATES = ("new", "opened")


async def unsnooze_due(conn: Connection, user_id: UUID, now: datetime) -> int:
    """Snoozes expire after 24 h: the alert comes back as `new`."""
    status = await conn.execute(
        "update alerts set state = 'new', snoozed_until = null "
        "where user_id = $1 and state = 'snoozed' and snoozed_until is not null and snoozed_until <= $2",
        user_id,
        now,
    )
    return int(status.split()[-1])


async def get_alert(conn: Connection, user_id: UUID, alert_id: UUID, *, lock: bool = False) -> Any | None:
    if lock:
        return await conn.fetchrow(
            "select * from alerts where user_id = $1 and id = $2 for update", user_id, alert_id
        )
    return await conn.fetchrow("select * from alerts where user_id = $1 and id = $2", user_id, alert_id)
