"""Deterministic offline provider (tests, CI, and "no API key" runs).

Handlers are keyed by `job`; each receives the `ProviderRequest` (including `hints`, the structured inputs the
caller passed for exactly this purpose) and returns the response text. The default handler returns a clearly
labelled echo, so nothing ever mistakes mock output for model output. Failure injection (`script`) drives the
retry/chaos tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.modules.llm.types import ProviderError, ProviderRequest, ProviderResult

Handler = Callable[[ProviderRequest], str]

_HANDLERS: dict[str, Handler] = {}


def register_mock_handler(job: str, handler: Handler) -> None:
    """Modules register deterministic stand-ins for their jobs (e.g. research registers `brief_claims`)."""
    _HANDLERS[job] = handler


def _default(req: ProviderRequest) -> str:
    if req.json_schema is not None:
        return json.dumps({})
    return "[mock] " + (req.messages[-1].content[:120] if req.messages else "")


def _approx_tokens(text: str) -> int:
    return max(1, len(text.split()) * 4 // 3)


class MockProvider:
    name = "mock"

    def __init__(
        self,
        handlers: dict[str, Handler] | None = None,
        script: list[ProviderError | str | None] | None = None,
    ) -> None:
        """`script`: one entry per call, consumed in order: an exception to raise, a string to return as the
        raw response text, or None to fall through to the normal handler."""
        self._handlers = dict(handlers or {})
        self._script = list(script or [])
        self.calls: list[ProviderRequest] = []

    async def aclose(self) -> None:
        return None

    async def complete(self, req: ProviderRequest) -> ProviderResult:
        self.calls.append(req)
        if self._script:
            step: Any = self._script.pop(0)
            if isinstance(step, ProviderError):
                raise step
            if isinstance(step, str):
                return self._result(req, step)
        handler = self._handlers.get(req.job) or _HANDLERS.get(req.job) or _default
        return self._result(req, handler(req))

    @staticmethod
    def _result(req: ProviderRequest, text: str) -> ProviderResult:
        tin = sum(_approx_tokens(m.content) for m in req.messages)
        return ProviderResult(
            text=text, tokens_in=tin, tokens_out=_approx_tokens(text), model=f"mock:{req.model}"
        )
