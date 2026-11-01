from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    description: str
    target: str | float | int
    actual: str | float | int
    status: Literal["pass", "fail"]


class EvalsLatest(BaseModel):
    """`items` / `generated_at` / `thresholds_met` are the contract; the rest documents the run."""

    model_config = ConfigDict(extra="allow")
    generated_at: str
    thresholds_met: bool
    items: list[EvalItem]
    not_measured: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
