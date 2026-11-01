from __future__ import annotations

from pydantic import Field

from app.core.schemas import ResponseModel


class StepOut(ResponseModel):
    name: str
    ms: int
    outcome: str  # ok | blocked | error
    detail: str


class TraceOut(ResponseModel):
    id: str
    trace_id: str
    name: str
    model: str
    mode: str
    duration_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    outcome: str  # ok | blocked | error
    kind: str  # brief | chat | ingest | other
    scope: str  # user | system (system = no user data, e.g. a quarantined filing chunk)
    created_at: str
    steps: list[StepOut] = Field(default_factory=list)


class TraceList(ResponseModel):
    items: list[TraceOut]
    next_cursor: str | None
