"""`GET /v1/audit/traces`, `GET /v1/audit/traces/{id}` (CONTRACT: Audit)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal

from fastapi import APIRouter, FastAPI, Query

from app.core.deps import CurrentUser, UserConn
from app.core.logging import get_logger
from app.modules.audit import service
from app.modules.audit.quarantine import backfill_quarantine_traces
from app.modules.audit.schemas import TraceList, TraceOut

router = APIRouter(tags=["audit"])
log = get_logger("app.audit")


@router.get(
    "/audit/traces",
    response_model=TraceList,
    summary="Your recent brief/chat traces (+ system injection blocks)",
)
async def traces(
    user: CurrentUser,
    conn: UserConn,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    outcome: Literal["ok", "blocked", "error"] | None = None,
    kind: Literal["brief", "chat", "ingest"] | None = None,
) -> TraceList:
    items, nxt = await service.list_traces(conn, limit, cursor, outcome, kind)
    return TraceList(items=items, next_cursor=nxt)


@router.get("/audit/traces/{trace_ref}", response_model=TraceOut, summary="One trace with its steps")
async def trace(trace_ref: str, user: CurrentUser, conn: UserConn) -> TraceOut:
    return await service.get_trace(conn, trace_ref)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create traces for quarantined chunks that have none yet (best effort, never blocks startup)."""
    task: asyncio.Task[int] | None = None
    if os.environ.get("AUDIT_BACKFILL", "1") != "0":

        async def _run() -> int:
            try:
                return await backfill_quarantine_traces()
            except Exception:
                log.exception("quarantine trace backfill failed")
                return 0

        task = asyncio.create_task(_run())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
