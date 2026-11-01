"""Style card: the only thing that differs between users' briefs (order, jargon, depth). Facts never change."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.core.db import Connection

SECTION_KEYS: tuple[str, ...] = ("trend", "bal", "val", "moat", "risk", "cat", "bb")
DEFAULT_ORDER = list(SECTION_KEYS)
CONSERVATIVE_ORDER = ["bal", "risk", "moat", "val", "trend", "cat", "bb"]  # design: Priya
GROWTH_ORDER = ["trend", "cat", "val", "risk", "moat", "bal", "bb"]  # design: Alex


class StyleCard(BaseModel):
    order: list[str] = Field(default_factory=lambda: list(DEFAULT_ORDER))
    jargon: Literal[1, 2, 3] = 2  # 1 = plain language with hints, 3 = technical
    depth: Literal["tldr", "standard", "deep"] = "standard"
    tone: str = "neutral"
    metric: str | None = None  # pinned metric preference (UI tile order)

    @field_validator("order")
    @classmethod
    def _complete_order(cls, v: list[str]) -> list[str]:
        seen = [k for k in dict.fromkeys(v) if k in SECTION_KEYS]
        return seen + [k for k in SECTION_KEYS if k not in seen]  # unknown keys dropped, missing appended

    def style_hash(self) -> str:
        canon = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:16]


def default_style_from_profile(experience: str | None, goals: list[str] | None) -> StyleCard:
    exp = (experience or "").strip().lower()
    goals_l = {g.lower() for g in (goals or [])}
    if exp == "beginner" or goals_l & {"income", "preservation"}:
        order = CONSERVATIVE_ORDER
    elif "growth" in goals_l:
        order = GROWTH_ORDER
    else:
        order = DEFAULT_ORDER
    jargon: Literal[1, 2, 3] = 1 if exp == "beginner" else 3 if exp in ("advanced", "expert") else 2
    depth: Literal["tldr", "standard", "deep"] = "standard" if exp in ("", "beginner") else "deep"
    return StyleCard(order=list(order), jargon=jargon, depth=depth)


async def load_style_card(conn: Connection, user_id: UUID) -> StyleCard:
    """`user_features.style_card` (written by the personalization service) else a default from the profile."""
    raw: Any = await conn.fetchval(
        "select value from user_features where user_id = $1 and key = 'style_card'", user_id
    )
    if isinstance(raw, dict):
        try:
            return StyleCard.model_validate(raw)
        except ValueError:
            pass
    prof = await conn.fetchrow("select experience, goals from profiles where user_id = $1", user_id)
    if prof is None:
        return StyleCard()
    return default_style_from_profile(prof["experience"], list(prof["goals"] or []))
