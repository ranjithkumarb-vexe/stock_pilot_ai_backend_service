"""First-sight user provisioning (the auth user record is upserted on first authenticated request).

Deleted accounts (SEC-13): `DELETE /v1/account` leaves a tombstone (hash of the id + deletion time; migration 0900). A token
issued at or before that moment can never provision the account again; only a token issued afterwards (a genuinely new
sign-in) can. A process-local mirror of the tombstone closes the window between the deletion and its commit.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime
from uuid import UUID

from app.core.auth import AuthUser
from app.core.db import user_tx
from app.core.errors import unauthenticated

_MAX_CACHED = 10_000


class KnownUsers:
    """Tiny TTL cache so the existence check is not run on every request."""

    def __init__(self, ttl_s: float = 300.0) -> None:
        self._ttl = ttl_s
        self._seen: OrderedDict[UUID, float] = OrderedDict()
        self._deleted: OrderedDict[UUID, float] = OrderedDict()  # user id -> deletion time (epoch seconds)

    def forget(self, user_id: UUID) -> None:
        self._seen.pop(user_id, None)

    def mark_deleted(self, user_id: UUID, deleted_at: datetime) -> None:
        """Called by the account-deletion endpoint: drop the cache entry and refuse older tokens immediately."""
        self._seen.pop(user_id, None)
        self._deleted[user_id] = deleted_at.timestamp()
        self._deleted.move_to_end(user_id)
        while len(self._deleted) > _MAX_CACHED:
            self._deleted.popitem(last=False)

    @staticmethod
    def _predates(user: AuthUser, deleted_at: float) -> bool:
        # a token without `iat` cannot prove it was issued after the deletion: fail closed
        return user.issued_at is None or user.issued_at <= deleted_at

    async def ensure(self, user: AuthUser) -> None:
        local = self._deleted.get(user.user_id)
        if local is not None and self._predates(user, local):
            raise unauthenticated("Account deleted")
        now = time.monotonic()
        seen_at = self._seen.get(user.user_id)
        if seen_at is not None and now - seen_at < self._ttl:
            return
        # RLS allows a user to insert exactly their own row
        async with user_tx(user.user_id) as conn:
            tombstone = await conn.fetchval("select my_account_deleted_at()")
            if tombstone is not None and self._predates(user, tombstone.timestamp()):
                raise unauthenticated("Account deleted")
            await conn.execute(
                "insert into users (id, email) values ($1, $2) on conflict (id) do nothing",
                user.user_id,
                user.email,
            )
        self._seen[user.user_id] = now
        self._seen.move_to_end(user.user_id)
        while len(self._seen) > _MAX_CACHED:
            self._seen.popitem(last=False)
