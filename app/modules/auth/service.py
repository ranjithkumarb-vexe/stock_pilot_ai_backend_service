"""User directory operations behind the auth endpoints."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.core.audit import write_audit
from app.core.db import Connection, user_tx
from app.core.personas import PERSONAS, initials, user_id_for_email
from app.modules.auth.schemas import MeResponse


def to_me(row: Any) -> MeResponse:
    return MeResponse(
        user_id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        initials=initials(row["display_name"], row["email"]),
        onboarded=row["onboarded_at"] is not None,
        persona_id=row["persona_id"],
    )


async def fetch_me(conn: Connection, user_id: UUID) -> MeResponse:
    row = await conn.fetchrow(
        "select id, email, display_name, persona_id, onboarded_at from users where id = $1", user_id
    )
    if row is None:  # cannot happen after get_current_user, but never 500 on it
        raise LookupError("user row missing")
    return to_me(row)


async def dev_login_user(persona: str, email: str | None) -> MeResponse:
    """Create-or-find the user for a dev-login persona. Personas have deterministic ids and exist
    immediately (seed data, BE-04, fills their portfolios later)."""
    if persona in PERSONAS:
        p = PERSONAS[persona]
        user_id, mail, name, persona_id, onboarded = (
            p.user_id,
            p.email,
            p.display_name,
            p.key,
            p.onboarded,
        )
    else:
        if email:
            user_id, mail = user_id_for_email(email), email.strip().lower()
        else:
            user_id = uuid4()
            mail = f"new-{user_id.hex[:8]}@stockpilot.dev"
        name, persona_id, onboarded = None, None, False
    async with user_tx(user_id) as conn:
        await conn.execute(
            "select clear_my_tombstone()"
        )  # dev-login = explicit fresh start (SEC-13, dev only)
        await conn.execute(
            """
            insert into users (id, email, display_name, persona_id, onboarded_at)
            values ($1, $2, $3, $4, case when $5::boolean then now() end)
            on conflict (id) do nothing
            """,
            user_id,
            mail,
            name,
            persona_id,
            onboarded,
        )
        return await fetch_me(conn, user_id)


async def delete_account(conn: Connection, user_id: UUID, now: datetime) -> None:
    """Skeleton account deletion (stream A/BE-05 extends with export hooks and Supabase auth deletion).

    1. audit the deletion (keyed by a hash of the id, not the id itself),
    2. delete the `users` row — every user-owned table (incl. embeddings) cascades via FK,
    3. anonymise the user's remaining audit rows (audit_log has no FK and is append-only),
    4. leave a tombstone (hash of the id + `now`, migration 0900) so the still-valid JWT cannot re-provision the account
       (SEC-13).
    """
    await write_audit(
        conn,
        user_id=user_id,
        action="account.delete",
        entity="user",
        entity_id=None,
        details={"user_hash": hashlib.sha256(str(user_id).encode()).hexdigest()[:16]},
    )
    await conn.execute("delete from users where id = $1", user_id)
    await conn.fetchval("select anonymize_audit_log($1)", user_id)
    await conn.execute("select tombstone_my_account($1)", now)
