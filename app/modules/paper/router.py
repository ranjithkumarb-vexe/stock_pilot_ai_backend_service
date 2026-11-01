"""Orders API (CONTRACT): preview, create (Idempotency-Key required), list, cancel.

All trades are simulated in the paper ledger. A background sweeper (module lifespan) fills queued orders at the
open, fills working limit orders when price crosses, expires day orders at the close and records equity
snapshots; set `PAPER_SWEEP_INTERVAL_S=0` to disable it (tests drive `service.process_orders` directly).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Query, Response
from fastapi.responses import JSONResponse

from app.core.db import system_tx
from app.core.deps import CurrentUser, UserConn
from app.core.errors import validation_error
from app.core.logging import get_logger
from app.modules.paper import service
from app.modules.paper.context import EngineCtx, build_context
from app.modules.paper.schemas import (
    OrderList,
    OrderOut,
    OrderRequest,
    PlaceResponse,
    PreviewResponse,
    dump,
)

log = get_logger("app.paper.router")
router = APIRouter(tags=["orders"])

DEFAULT_SWEEP_S = 5.0


def _intent(body: OrderRequest) -> service.OrderIntent:
    return service.OrderIntent(
        body.symbol, body.side, body.type, body.qty, body.limit_price, body.proposal_id, body.override
    )


def _key(raw: str | None) -> str:
    if not raw:
        raise validation_error(
            "The Idempotency-Key header is required (a UUID)", {"header": "Idempotency-Key"}
        )
    try:
        return str(UUID(raw.strip()))
    except ValueError as exc:
        raise validation_error("Idempotency-Key must be a UUID", {"header": "Idempotency-Key"}) from exc


@router.post(
    "/orders/preview", response_model=PreviewResponse, summary="Pre-trade checks (Pass / Note / Blocked)"
)
async def preview(body: OrderRequest, user: CurrentUser, conn: UserConn, ctx: EngineCtx) -> dict[str, object]:
    return dump(PreviewResponse, await service.preview_order(conn, user.user_id, _intent(body), ctx))


@router.post("/orders", response_model=PlaceResponse, summary="Place a paper order")
async def create_order(
    body: OrderRequest,
    user: CurrentUser,
    conn: UserConn,
    ctx: EngineCtx,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    result = await service.place_order(conn, user.user_id, _intent(body), _key(idempotency_key), ctx)
    payload = dump(
        PlaceResponse,
        {"order": result.order, "fill": result.fill, "checks": result.checks, "blocked": result.blocked},
    )
    headers = {"Idempotent-Replay": "true"} if result.replay else {}
    return JSONResponse(payload, status_code=200 if result.replay else 201, headers=headers)


@router.get("/orders", response_model=OrderList, summary="My orders (newest first)")
async def list_orders(
    user: CurrentUser,
    conn: UserConn,
    status: Annotated[
        str | None, Query(pattern="^(queued|working|filled|cancelled|expired|rejected)$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> dict[str, object]:
    return dump(
        OrderList, await service.list_orders(conn, user.user_id, status=status, limit=limit, cursor=cursor)
    )


@router.delete("/orders/{order_id}", response_model=OrderOut, summary="Cancel a queued or working order")
async def cancel(order_id: UUID, user: CurrentUser, conn: UserConn, ctx: EngineCtx) -> Response:
    order = await service.cancel_order(conn, user.user_id, order_id, ctx)
    return JSONResponse(OrderOut.model_validate(order).model_dump(mode="json"))


# --------------------------------------------------------------------------------------------------
# background sweeper
# --------------------------------------------------------------------------------------------------


async def _sweep_loop(app: FastAPI, interval: float) -> None:
    snapshot_every = 15 * 60.0
    last_snapshot = -snapshot_every
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.sleep(interval)
            ctx = build_context(app.state)
            async with system_tx() as conn:
                stats = await service.process_orders(conn, ctx)
                if loop.time() - last_snapshot >= snapshot_every and ctx.market_now().is_open:
                    await service.snapshot_all(conn, ctx)
                    last_snapshot = loop.time()
            if any(stats.values()):
                log.info("order sweep", extra={"stats": stats})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("order sweep failed", extra={"error": type(exc).__name__})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        interval = float(os.environ.get("PAPER_SWEEP_INTERVAL_S", DEFAULT_SWEEP_S))
    except ValueError:
        interval = DEFAULT_SWEEP_S
    task = asyncio.create_task(_sweep_loop(app, interval)) if interval > 0 else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
