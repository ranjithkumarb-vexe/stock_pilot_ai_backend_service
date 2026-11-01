"""Onboarding endpoints. `GET /onboarding/questions` is public; `POST /onboarding` creates the workspace."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.db import get_db
from app.core.deps import CurrentUser, UserConn
from app.modules.onboarding import content, service
from app.modules.onboarding.schemas import OnboardingRequest, OnboardingResponse
from app.modules.paper.context import EngineCtx
from app.modules.tradingcommon.disclaimer import DISCLAIMER_TEXT

router = APIRouter(tags=["onboarding"])


@router.get("/onboarding/questions", summary="Questionnaire, tiers and option lists (public)")
async def questions() -> dict[str, Any]:
    async with get_db().pool.acquire() as conn:
        sectors = [
            r["sector"]
            for r in await conn.fetch(
                "select distinct sector from tickers where in_universe and sector is not null order by sector"
            )
        ]
    return {
        "questions": [
            {
                "id": i + 1,
                "text": text,
                "options": [{"label": label, "score": score} for label, score in opts],
            }
            for i, (text, opts) in enumerate(content.QUESTIONS)
        ],
        "tiers": [{"tier": t, "label": content.TIER_LABELS[t]} for t in range(1, 6)],
        "tier_rules": {str(t): content.tier_rules(t).to_dict() for t in range(1, 6)},
        "horizons": list(content.HORIZONS),
        "experience_levels": list(content.EXPERIENCE_LEVELS),
        "goals": list(content.GOALS),
        "sectors": sectors,
        "esg_exclusions": list(content.ESG_EXCLUSIONS),
        "disclaimer": DISCLAIMER_TEXT,
    }


@router.post(
    "/onboarding",
    response_model=OnboardingResponse,
    summary="Submit the questionnaire and create the workspace",
)
async def onboard(
    body: OnboardingRequest, user: CurrentUser, conn: UserConn, ctx: EngineCtx
) -> dict[str, Any]:
    return await service.complete_onboarding(conn, user.user_id, body, ctx)
