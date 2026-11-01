"""Response models of the feed / why endpoints (CONTRACT: Feed row)."""

from __future__ import annotations

from typing import Any, Literal

from app.core.schemas import ResponseModel


class WhyFeature(ResponseModel):
    name: str
    weight: float
    detail: str


class WhyRule(ResponseModel):
    rule: str
    passed: bool


class WhyOut(ResponseModel):
    summary: str
    top_features: list[WhyFeature]
    rules_checked: list[WhyRule]
    sources: list[str]
    basis: Literal["profile", "behaviour", "cold_start"]


class FeedItemOut(ResponseModel):
    kind: Literal["alert", "proposal", "briefing"]
    id: str
    pinned: bool
    score: float
    item: dict[str, Any]
    why: WhyOut


class FeedPage(ResponseModel):
    items: list[FeedItemOut]
    next_cursor: str | None = None
    total: int
