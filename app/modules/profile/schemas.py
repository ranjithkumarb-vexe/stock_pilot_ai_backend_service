from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.core.schemas import ApiModel
from app.modules.onboarding.schemas import Experience, Goal, Horizon


class ProfilePatch(ApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    horizon: Horizon | None = None
    goals: list[Goal] | None = Field(default=None, min_length=1, max_length=3)
    experience: Experience | None = None
    region: Literal["US"] | None = None
    currency: Literal["USD"] | None = None
    esg_exclusions: list[str] | None = Field(default=None, max_length=12)
    likes: list[str] | None = Field(default=None, max_length=12)
    excludes: list[str] | None = Field(default=None, max_length=12)
    tone: Literal["Plain language", "Direct", "Neutral"] | None = None
    depth: Literal["tldr", "standard", "deep"] | None = None
    jargon: int | None = Field(default=None, ge=1, le=3)
    metric: str | None = Field(default=None, max_length=40)
    language: Literal["English"] | None = None

    @model_validator(mode="after")
    def _nonempty(self) -> ProfilePatch:
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        return self


class RulesBody(ApiModel):
    pos: Decimal = Field(gt=0, le=100)
    sec: Decimal = Field(gt=0, le=100)
    dd: Decimal = Field(gt=0, le=100)
    trades: int = Field(ge=1, le=100)
    cash: Decimal = Field(ge=0, le=100)
