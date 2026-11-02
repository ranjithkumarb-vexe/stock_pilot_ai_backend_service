"""Pagination cursor parsing shared by endpoints whose cursor is a `created_at` timestamp (SEC-06)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import validation_error

_MAX_LEN = 64


def parse_timestamp_cursor(cursor: str) -> datetime:
    """ISO-8601 timestamp (`Z` suffix allowed) -> aware datetime. Anything else is a 422 `validation_error`, never a 500.

    A cursor without a UTC offset is read as UTC. The value is only ever used as a bound parameter, but a cursor that
    cannot be parsed must not reach the database layer.
    """
    if len(cursor) > _MAX_LEN or "\x00" in cursor:
        raise validation_error("Invalid cursor")
    try:
        parsed = datetime.fromisoformat(cursor.strip().replace("Z", "+00:00"))
    except ValueError:
        raise validation_error("Invalid cursor") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
