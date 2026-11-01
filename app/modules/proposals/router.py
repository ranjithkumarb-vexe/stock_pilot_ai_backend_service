"""Proposals API (CONTRACT): list, get, decide (approve | reject | modify), refresh."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import model_validator

from app.core.deps import ClockDep, CurrentUser, UserConn
from app.core.schemas import ApiModel
from app.modules.paper.context import EngineCtx
from app.modules.paper.hooks import register_after_trade
from app.modules.proposals import service

router = APIRouter(tags=["proposals"])

# keep proposals in step with the portfolio after every fill / cancel / expiry (paper engine hook)
register_after_trade(service.refresh_after_trade)

Status = Literal["pending", "approved", "rejected", "modified", "expired"]


class DecisionBody(ApiModel):
    decision: Literal["approve", "reject", "modify"]
    reason: Literal["too_risky", "disagree", "timing", "already_know"] | None = None
    qty: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> DecisionBody:
        if self.decision == "modify" and self.qty is None:
            raise ValueError("qty is required when modifying")
        return self


@router.post("/proposals/refresh", summary="Generate / expire proposals from the risk engine (idempotent)")
async def refresh(user: CurrentUser, conn: UserConn, ctx: EngineCtx) -> dict[str, Any]:
    return await service.refresh(conn, user.user_id, ctx)


@router.get("/proposals", summary="My proposals")
async def list_proposals(
    user: CurrentUser,
    conn: UserConn,
    clock: ClockDep,
    status: Status | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return {
        "items": await service.list_proposals(conn, user.user_id, clock.now(), status, limit),
        "next_cursor": None,
    }


@router.get("/proposals/{proposal_id}", summary="One proposal")
async def get_proposal(
    proposal_id: UUID, user: CurrentUser, conn: UserConn, clock: ClockDep
) -> dict[str, Any]:
    return await service.get_proposal(conn, user.user_id, proposal_id, clock.now())


@router.post("/proposals/{proposal_id}/decision", summary="Approve, reject (with reason) or modify the size")
async def decide(
    proposal_id: UUID, body: DecisionBody, user: CurrentUser, conn: UserConn, ctx: EngineCtx
) -> dict[str, Any]:
    return await service.decide(
        conn, user.user_id, proposal_id, body.decision, ctx, reason=body.reason, qty=body.qty
    )
