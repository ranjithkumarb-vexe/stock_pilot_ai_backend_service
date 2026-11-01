"""Portfolio, risk and performance endpoints (CONTRACT)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, UserConn
from app.modules.paper.context import EngineCtx
from app.modules.portfolio import service

router = APIRouter(tags=["portfolio"])


@router.get("/portfolio", summary="Positions, sectors, concentration, day change")
async def portfolio(user: CurrentUser, conn: UserConn, ctx: EngineCtx) -> dict[str, Any]:
    return await service.get_portfolio(conn, user.user_id, ctx)


@router.get("/portfolio/risk", summary="Risk metrics and rule status from the deterministic risk engine")
async def risk(user: CurrentUser, conn: UserConn, ctx: EngineCtx) -> dict[str, Any]:
    return await service.get_risk(conn, user.user_id, ctx)


@router.get("/portfolio/performance", summary="Equity curve vs SPY and performance stats")
async def performance(
    user: CurrentUser,
    conn: UserConn,
    ctx: EngineCtx,
    days: Annotated[int | None, Query(ge=1, le=3650)] = None,
) -> dict[str, Any]:
    return await service.get_performance(conn, user.user_id, ctx, days)
