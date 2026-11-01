"""Documents can never create memories (SPEC 11.2 LLM04; T-SEC-05).

Memories derive only from the user's own actions. Writers call `ensure_memory_source_allowed()` with the origin
of the text; anything fetched (filings, news, retrieved chunks, tool output, model output) is refused.
`assert_no_memory_from_document()` is the matching test helper for the database.
"""

from __future__ import annotations

from uuid import UUID

from app.core.db import Connection

ALLOWED_SOURCES = frozenset({"onboarding", "user_event", "user_action", "user_message", "user_edit"})
FORBIDDEN_SOURCES = frozenset(
    {"document", "filing", "news", "retrieved_chunk", "tool_output", "model_output", "web"}
)


class MemoryPoisoningError(PermissionError):
    """A memory write was attempted from untrusted content."""


def ensure_memory_source_allowed(source: str) -> None:
    if source in FORBIDDEN_SOURCES or source not in ALLOWED_SOURCES:
        raise MemoryPoisoningError(f"memories may not be created from source {source!r}")


async def count_memories(conn: Connection, user_id: UUID) -> int:
    return int(await conn.fetchval("select count(*) from memories where user_id = $1", user_id))


async def assert_no_memory_from_document(conn: Connection, user_id: UUID, baseline: int) -> None:
    """Raise AssertionError if the user's memory count grew after fetched text was processed."""
    now = await count_memories(conn, user_id)
    if now != baseline:
        raise AssertionError(
            f"memory count changed {baseline} -> {now} while processing documents (T-SEC-05)"
        )
