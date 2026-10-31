"""Append-only audit trail helper. Every write endpoint calls `write_audit` inside its user transaction."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.logging import trace_id_var


async def write_audit(
    conn: Connection,
    *,
    user_id: UUID,
    action: str,
    entity: str,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Insert one `audit_log` row (RLS: only the acting user's rows). Returns the row id.

    Never put tokens, passwords or free-text user content in `details`; the trace id is added for correlation.
    """
    payload = {"trace_id": trace_id_var.get(), **(details or {})}
    row_id = await conn.fetchval(
        "insert into audit_log (user_id, action, entity, entity_id, details) "
        "values ($1, $2, $3, $4, $5) returning id",
        user_id,
        action,
        entity,
        entity_id,
        payload,
    )
    return int(row_id)
