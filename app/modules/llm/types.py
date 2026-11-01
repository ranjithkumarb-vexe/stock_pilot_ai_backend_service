"""Request/response types for the single LLM entry point (SPEC 7.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

DataClass = Literal["public", "user"]
Mode = Literal["standard", "contributor"]
Effort = Literal["minimal", "low", "medium", "high"]  # "none" is invalid (HTTP 400 upstream, SPEC 7.3)
VALID_EFFORTS: frozenset[str] = frozenset({"minimal", "low", "medium", "high"})


class PolicyViolationError(Exception):
    """A call that must never be made (e.g. non-public data in Contributor mode, T-SEC-06)."""


class ProviderError(Exception):
    """A provider call failed. `retriable` drives the client's backoff loop."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retriable: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retriable = retriable
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(slots=True)
class LlmRequest:
    job: str
    messages: list[Message]
    data_class: DataClass
    user_id: UUID | None = None
    response_model: type[BaseModel] | None = None
    max_tokens: int = 2000
    temperature: float = 0.2
    effort: Effort | None = None  # None -> route default
    prompt_version: str | None = None
    #: structured inputs for the deterministic MockProvider only; never serialised to a remote provider
    hints: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderRequest:
    job: str
    model: str
    mode: Mode
    messages: list[Message]
    effort: Effort
    max_tokens: int
    temperature: float
    json_schema: dict[str, Any] | None
    hints: dict[str, Any]


@dataclass(slots=True)
class ProviderResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str


@dataclass(slots=True)
class LlmResponse:
    text: str = ""
    parsed: BaseModel | None = None
    model: str = ""
    mode: Mode = "standard"
    provider: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 0
    degraded: bool = False
    degraded_reason: str | None = None
    budget_alert: bool = False

    @property
    def ok(self) -> bool:
        return not self.degraded
