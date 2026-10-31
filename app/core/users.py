"""First-sight user provisioning (the auth user record is upserted on first authenticated request)."""

from __future__ import annotations

import time
from collections import OrderedDict
from uuid import UUID

from app.core.auth import AuthUser
from app.core.db import user_tx

_MAX_CACHED = 10_000


class KnownUsers:
    """Tiny TTL cache so the existence check is not run on every request."""

    def __init__(self, ttl_s: float = 300.0) -> None:
        self._ttl = ttl_s
        self._seen: OrderedDict[UUID, float] = OrderedDict()

    def forget(self, user_id: UUID) -> None:
        self._seen.pop(user_id, None)

    async def ensure(self, user: AuthUser) -> None:
        now = time.monotonic()
        seen_at = self._seen.get(user.user_id)
        if seen_at is not None and now - seen_at < self._ttl:
            return
        # RLS allows a user to insert exactly their own row
        async with user_tx(user.user_id) as conn:
            await conn.execute(
                "insert into users (id, email) values ($1, $2) on conflict (id) do nothing",
                user.user_id,
                user.email,
            )
        self._seen[user.user_id] = now
        self._seen.move_to_end(user.user_id)
        while len(self._seen) > _MAX_CACHED:
            self._seen.popitem(last=False)
