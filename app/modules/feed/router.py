"""Feed + "why" endpoints. The lifespan attaches the bandit to the proposal workflow (decision hook)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Query

from app.core.deps import ClockDep, CurrentUser, UserConn
from app.core.errors import validation_error
from app.modules.feed.schemas import FeedPage, WhyOut
from app.modules.paper.context import EngineCtx
from app.modules.ranking import bandit, service

router = APIRouter(tags=["feed"])


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    bandit.register_hooks()  # proposal approve / modify / reject -> Beta arm update (SPEC 10.3 C)
    yield


@router.get("/feed", response_model=FeedPage, summary="Ranked feed: alerts, proposals and the daily briefing")
async def get_feed(
    user: CurrentUser,
    conn: UserConn,
    clock: ClockDep,
    ctx: EngineCtx,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = service.DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=16)] = None,
) -> FeedPage:
    offset = 0
    if cursor:
        if not cursor.isdigit():
            raise validation_error("Invalid cursor")
        offset = int(cursor)
    res = await service.build_feed(conn, user.user_id, ctx, clock.now(), limit=limit, offset=offset)
    return FeedPage.model_validate({"items": res.items, "next_cursor": res.next_cursor, "total": res.total})


@router.get("/why/{output_type}/{output_id}", response_model=WhyOut, summary="Why am I seeing this?")
async def get_why(
    output_type: str, output_id: str, user: CurrentUser, conn: UserConn, clock: ClockDep, ctx: EngineCtx
) -> Any:
    return await service.explain(conn, user.user_id, ctx, clock.now(), output_type, output_id)
