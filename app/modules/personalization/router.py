"""Personalization module wiring: lifespan (feature scheduler) + a dev-only recompute endpoint.

`app.state.feature_scheduler` is what `POST /v1/events` pings (`mark_dirty`). The ranker / feed / why endpoints
(BE-16) will be added to this router later.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.core.deps import ClockDep, CurrentUser, SettingsDep, UserConn
from app.core.errors import not_found
from app.core.schemas import ResponseModel
from app.modules.personalization.recompute import recompute_features
from app.modules.personalization.scheduler import FeatureScheduler

router = APIRouter(tags=["personalization"])


class RecomputeResponse(ResponseModel):
    n_events: int
    revealed_tier: float | None
    gap_days: int
    memories: dict[str, int]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    scheduler = FeatureScheduler(app.state.clock)
    app.state.feature_scheduler = scheduler
    autostart = os.environ.get("PERSONALIZATION_AUTOSTART", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    if autostart:
        scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


@router.post(
    "/dev/recompute-features",
    response_model=RecomputeResponse,
    summary="Dev only: recompute my learned features now",
)
async def dev_recompute(
    user: CurrentUser, conn: UserConn, clock: ClockDep, settings: SettingsDep
) -> RecomputeResponse:
    if settings.auth_mode != "dev":
        raise not_found()
    result = await recompute_features(user.user_id, now=clock.now(), conn=conn)
    return RecomputeResponse(
        n_events=result.n_events,
        revealed_tier=result.revealed_tier,
        gap_days=result.gap_days,
        memories=result.memories,
    )
