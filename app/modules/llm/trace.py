"""Trace recorder other modules import (FR-16): one `traces` row + ordered `trace_steps` rows per request/job.

    async with trace_scope("brief:NVDA", user_id=uid) as t:
        with t.step("retrieve", chunks=7):
            ...
        t.add("verify", ms=3, outcome="ok", detail={"unverified": 0})
    # flushed on exit (outcome "error" if the block raised)

The `LlmClient` adds an `llm:<job>` step and token/cost totals to the active recorder automatically (found via
`current_trace()` or passed explicitly). Never put prompts, user free text or secrets in `detail`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal
from uuid import UUID

from app.core.clock import Clock, SystemClock
from app.core.db import get_db, system_tx, user_tx
from app.core.logging import get_logger, trace_id_var

log = get_logger("app.llm.trace")
Outcome = Literal["ok", "blocked", "error"]
_current: ContextVar[TraceRecorder | None] = ContextVar("current_trace", default=None)


def current_trace() -> TraceRecorder | None:
    return _current.get()


@dataclass(slots=True)
class StepRecord:
    name: str
    ms: int
    outcome: str
    detail: dict[str, Any]


class _StepCtx:
    def __init__(self, rec: TraceRecorder, name: str, detail: dict[str, Any]) -> None:
        self._rec, self._name, self.detail = rec, name, detail
        self._t0 = rec.clock.monotonic()
        self.outcome = "ok"

    def __enter__(self) -> _StepCtx:
        return self

    def __exit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        if et is not None:
            self.outcome = "error"
        ms = int((self._rec.clock.monotonic() - self._t0) * 1000)
        self._rec.add(self._name, ms, self.outcome, self.detail)


class TraceRecorder:
    def __init__(
        self,
        name: str,
        *,
        user_id: UUID | None = None,
        trace_id: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.name = name
        self.user_id = user_id
        self.trace_id = trace_id or trace_id_var.get()
        self.clock: Clock = clock or SystemClock()
        self.steps: list[StepRecord] = []
        self.model: str | None = None
        self.mode: str | None = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.outcome: Outcome = "ok"
        self.row_id: UUID | None = None
        self._t0 = self.clock.monotonic()

    def step(self, name: str, **detail: Any) -> _StepCtx:
        return _StepCtx(self, name, dict(detail))

    def add(self, name: str, ms: int = 0, outcome: str = "ok", detail: dict[str, Any] | None = None) -> None:
        self.steps.append(StepRecord(name, int(ms), outcome, dict(detail or {})))

    def add_usage(self, *, model: str, mode: str, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self.model, self.mode = model, mode
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost_usd

    def set_outcome(self, outcome: Outcome) -> None:
        self.outcome = outcome

    @property
    def duration_ms(self) -> int:
        return int((self.clock.monotonic() - self._t0) * 1000)

    async def flush(self) -> UUID | None:
        """Persist. Returns the `traces.id`, or None if there is no database (unit tests)."""
        try:
            get_db()
        except RuntimeError:
            return None
        cm = user_tx(self.user_id) if self.user_id else system_tx()
        try:
            async with cm as conn:
                row_id = await conn.fetchval(
                    "insert into traces (trace_id, user_id, name, model, mode, duration_ms, tokens_in, tokens_out, "
                    "cost_usd, outcome) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) returning id",
                    self.trace_id,
                    self.user_id,
                    self.name,
                    self.model,
                    self.mode,
                    self.duration_ms,
                    self.tokens_in,
                    self.tokens_out,
                    round(self.cost_usd, 6),
                    self.outcome,
                )
                if self.steps:
                    await conn.execute(
                        "insert into trace_steps (trace_ref, user_id, idx, name, ms, outcome, detail) "
                        "select $1, $2, i - 1, n, m, o, d::jsonb from unnest($3::text[], $4::int[], $5::text[], $6::text[]) "
                        "with ordinality as t(n, m, o, d, i)",
                        row_id,
                        self.user_id,
                        [s.name for s in self.steps],
                        [s.ms for s in self.steps],
                        [s.outcome for s in self.steps],
                        [_json(s.detail) for s in self.steps],
                    )
        except Exception:
            log.exception("trace flush failed")  # tracing must never break the request
            return None
        self.row_id = row_id
        return UUID(str(row_id))


def _json(d: dict[str, Any]) -> str:
    import orjson

    return orjson.dumps(d, default=str).decode()


@contextmanager
def use_trace(rec: TraceRecorder) -> Iterator[TraceRecorder]:
    token = _current.set(rec)
    try:
        yield rec
    finally:
        _current.reset(token)


@asynccontextmanager
async def trace_scope(
    name: str, *, user_id: UUID | None = None, clock: Clock | None = None, trace_id: str | None = None
) -> AsyncIterator[TraceRecorder]:
    rec = TraceRecorder(name, user_id=user_id, clock=clock, trace_id=trace_id)
    with use_trace(rec):
        try:
            yield rec
        except BaseException:
            rec.set_outcome("error")
            raise
        finally:
            await rec.flush()
