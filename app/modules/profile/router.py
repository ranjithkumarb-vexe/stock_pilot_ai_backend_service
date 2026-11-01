"""Profile controls (FR-09): view, edit, rules, reset, export, forget a memory."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response

from app.core.deps import ClockDep, CurrentUser, UserConn
from app.core.errors import validation_error
from app.modules.onboarding.content import ESG_EXCLUSIONS
from app.modules.paper.context import EngineCtx
from app.modules.profile import export, service
from app.modules.profile.schemas import ProfilePatch, RulesBody
from app.modules.risk import Rules

router = APIRouter(tags=["profile"])


async def _sectors(conn: Any) -> set[str]:
    return {
        r["sector"]
        for r in await conn.fetch(
            "select distinct sector from tickers where in_universe and sector is not null"
        )
    }


@router.get("/profile", summary="Profile, rules, learned interests, memories, bias mirrors")
async def get_profile(user: CurrentUser, conn: UserConn, ctx: EngineCtx) -> dict[str, Any]:
    return await service.get_profile_full(conn, user.user_id, ctx)


@router.patch("/profile", summary="Edit profile fields (partial)")
async def patch_profile(
    body: ProfilePatch, user: CurrentUser, conn: UserConn, clock: ClockDep, ctx: EngineCtx
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    sectors = await _sectors(conn)
    for key in ("likes", "excludes"):
        bad = [s for s in changes.get(key) or [] if s not in sectors]
        if bad:
            raise validation_error(f"Unknown sector in {key}", {"sectors": bad})
    if set(changes.get("likes") or []) & set(changes.get("excludes") or []):
        raise validation_error("A sector cannot be both liked and excluded")
    bad_esg = [e for e in changes.get("esg_exclusions") or [] if e not in ESG_EXCLUSIONS]
    if bad_esg:
        raise validation_error("Unknown ESG exclusion", {"values": bad_esg})
    await service.patch_profile(conn, user.user_id, clock.now(), changes, sectors)
    return await service.get_profile_full(conn, user.user_id, ctx)


@router.put(
    "/profile/rules", summary="Replace the risk rules (audited; loosening returns a reflective prompt)"
)
async def put_rules(body: RulesBody, user: CurrentUser, conn: UserConn, clock: ClockDep) -> dict[str, Any]:
    return await service.put_rules(
        conn, user.user_id, clock.now(), Rules(body.pos, body.sec, body.dd, body.trades, body.cash)
    )


@router.post("/profile/reset", summary="Forget everything learned (keeps profile, rules, portfolio)")
async def reset_profile(user: CurrentUser, conn: UserConn, clock: ClockDep) -> dict[str, Any]:
    cleared = await service.reset_learning(conn, user.user_id, clock.now())
    return {"status": "reset", "cleared": cleared}


@router.get("/profile/export", summary="Download all of my data as JSON")
async def export_profile(user: CurrentUser, conn: UserConn, clock: ClockDep) -> Response:
    data = await export.export_user_data(conn, user.user_id, clock.now())
    return Response(
        export.dumps(data),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="stockpilot-export.json"'},
    )


@router.delete("/profile/memories/{memory_id}", status_code=204, summary="Forget one memory")
async def delete_memory(memory_id: UUID, user: CurrentUser, conn: UserConn, clock: ClockDep) -> Response:
    await service.forget_memory(conn, user.user_id, clock.now(), memory_id)
    return Response(status_code=204)
