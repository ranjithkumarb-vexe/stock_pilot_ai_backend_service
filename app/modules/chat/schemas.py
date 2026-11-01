from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field

from app.core.schemas import ApiModel, ResponseModel


class ChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: UUID | None = None


class ChatResponse(ResponseModel):
    id: str
    thread_id: str
    answer: str
    citations: list[dict[str, Any]]
    refused: bool
    refusal_reason: str | None
    trace_id: str
    #: extras beyond the contract (the FE ignores unknown keys)
    intent: str
    grounded: bool  # False = "I could not find that in the sources" or a refusal


class HistoryItem(ResponseModel):
    id: str
    thread_id: str
    role: str
    text: str
    created_at: str
    refused: bool
    refusal_reason: str | None
    citations: list[dict[str, Any]]
    trace_id: str | None


class HistoryOut(ResponseModel):
    items: list[HistoryItem]
    next_cursor: str | None
