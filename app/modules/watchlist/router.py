"""Universe (public) and watchlist endpoints."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response

from app.core.db import get_db
from app.core.deps import CurrentUser, UserConn
from app.core.errors import validation_error
from app.modules.paper.context import EngineCtx
from app.modules.watchlist import service

router = APIRouter(tags=["watchlist"])
_SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,10}$")

SymbolPath = Annotated[str, Path(min_length=1, max_length=10)]


def _symbol(raw: str) -> str:
    sym = raw.strip().upper()
    if not _SYMBOL.match(sym):
        raise validation_error("Invalid ticker symbol", {"symbol": raw[:10]})
    return sym


@router.get("/universe", summary="Supported universe (public)")
async def universe() -> dict[str, Any]:
    async with get_db().pool.acquire() as conn:
        return {"items": await service.list_universe(conn)}


@router.get("/watchlist", summary="My watchlist with quotes")
async def get_watchlist(user: CurrentUser, conn: UserConn, ctx: EngineCtx) -> dict[str, Any]:
    return {"items": await service.list_watchlist(conn, user.user_id, ctx)}


@router.put("/watchlist/{symbol}", summary="Add a supported ticker (idempotent)")
async def put_watchlist(
    symbol: SymbolPath, user: CurrentUser, conn: UserConn, ctx: EngineCtx
) -> dict[str, Any]:
    return await service.add_symbol(conn, user.user_id, _symbol(symbol), ctx)


@router.delete("/watchlist/{symbol}", status_code=204, summary="Remove a ticker (idempotent)")
async def delete_watchlist(symbol: SymbolPath, user: CurrentUser, conn: UserConn) -> Response:
    await service.remove_symbol(conn, user.user_id, _symbol(symbol))
    return Response(status_code=204)
