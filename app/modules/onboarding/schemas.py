from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator

from app.core.schemas import ApiModel, ResponseModel

Horizon = Literal["Under 1 year", "1-3 years", "3-10 years", "10+ years"]
Experience = Literal["Beginner", "Intermediate", "Advanced"]
Goal = Literal["Growth", "Income", "Preservation"]


class OnboardingRequest(ApiModel):
    answers: list[int] = Field(
        min_length=6, max_length=6, description="the six option scores, in question order"
    )
    horizon: Horizon
    goals: list[Goal] = Field(min_length=1, max_length=3)
    experience: Experience
    likes: list[str] = Field(default_factory=list, max_length=12)
    excludes: list[str] = Field(default_factory=list, max_length=12)
    esg_exclusions: list[str] = Field(default_factory=list, max_length=12)
    watchlist: list[str] = Field(default_factory=list, max_length=30)
    region: str = "US"
    currency: str = "USD"
    starting_cash: Decimal = Field(default=Decimal(100000), ge=1000, le=10_000_000, decimal_places=2)
    disclaimer_ack: bool

    @field_validator("watchlist")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        out = [s.strip().upper() for s in v]
        if len(set(out)) != len(out):
            raise ValueError("watchlist contains duplicates")
        return out


class OnboardingResponse(ResponseModel):
    profile: dict[str, Any]
    risk_tier: int
    tier_label: str
    explanation: str
    rules: dict[str, float | int]
    watchlist: list[dict[str, Any]]
