"""Token/cost budgets backed by `llm_calls` rows (SPEC 7.3, 13; NFR-05): per-user daily cap, global alert and
hard stop. The Postgres store keeps tenant isolation: per-user numbers go through `user_tx`, the global total
through a SECURITY DEFINER function that returns only the sum (migration 0200)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.core.clock import Clock
from app.core.db import get_db, system_tx, user_tx
from app.core.logging import get_logger

log = get_logger("app.llm.budget")
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class UsageRecord:
    user_id: UUID | None
    trace_id: str | None
    job: str
    model: str
    mode: str
    data_class: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    outcome: str
    attempts: int = 1
    provider: str = ""
    prompt_version: str | None = None


class UsageStore(Protocol):
    async def user_spend_since(self, user_id: UUID, since: datetime) -> float: ...
    async def global_spend_since(self, since: datetime) -> float: ...
    async def record(self, rec: UsageRecord) -> None: ...


class InMemoryUsageStore:
    """Used by unit tests and when the database is not initialised."""

    def __init__(self, now: Clock | None = None) -> None:
        self.records: list[tuple[datetime, UsageRecord]] = []
        self._clock = now

    def _now(self) -> datetime:
        return self._clock.now() if self._clock else datetime.now(UTC)

    async def user_spend_since(self, user_id: UUID, since: datetime) -> float:
        return sum(r.cost_usd for t, r in self.records if r.user_id == user_id and t >= since)

    async def global_spend_since(self, since: datetime) -> float:
        return sum(r.cost_usd for t, r in self.records if t >= since)

    async def record(self, rec: UsageRecord) -> None:
        self.records.append((self._now(), rec))


_INSERT = (
    "insert into llm_calls (user_id, trace_id, job, model, mode, data_class, tokens_in, tokens_out, cost_usd, "
    "latency_ms, outcome, attempts, provider, prompt_version) "
    "values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)"
)


class PgUsageStore:
    async def user_spend_since(self, user_id: UUID, since: datetime) -> float:
        async with user_tx(user_id) as conn:
            v = await conn.fetchval(
                "select coalesce(sum(cost_usd), 0) from llm_calls where user_id = $1 and created_at >= $2",
                user_id,
                since,
            )
        return float(v)

    async def global_spend_since(self, since: datetime) -> float:
        async with get_db().pool.acquire() as conn:
            v = await conn.fetchval("select llm_spend_global($1)", since)
        return float(v)

    async def record(self, rec: UsageRecord) -> None:
        args = (
            rec.user_id, rec.trace_id, rec.job, rec.model, rec.mode, rec.data_class, rec.tokens_in,
            rec.tokens_out, rec.cost_usd, rec.latency_ms, rec.outcome, rec.attempts, rec.provider or None,
            rec.prompt_version,
        )  # fmt: skip
        if rec.user_id is None:
            async with system_tx() as conn:
                await conn.execute(_INSERT, *args)
        else:
            async with user_tx(rec.user_id) as conn:
                await conn.execute(_INSERT, *args)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    reason: str | None = None  # "user_daily_cap" | "global_hard_stop"
    alert: bool = False


class BudgetGuard:
    def __init__(
        self,
        store: UsageStore,
        clock: Clock,
        *,
        user_daily_usd: float,
        alert_usd: float,
        hard_stop_usd: float,
        global_cache_s: float = 10.0,
    ) -> None:
        self._store = store
        self._clock = clock
        self._user_daily = user_daily_usd
        self._alert = alert_usd
        self._hard = hard_stop_usd
        self._cache_s = global_cache_s
        self._cached: tuple[float, float] | None = None  # (monotonic, total)
        self._alerted = False

    def invalidate(self) -> None:
        self._cached = None

    async def _global_total(self) -> float:
        now = self._clock.monotonic()
        if self._cached is not None and now - self._cached[0] < self._cache_s:
            return self._cached[1]
        total = await self._store.global_spend_since(EPOCH)
        self._cached = (now, total)
        return total

    async def check(self, user_id: UUID | None) -> BudgetDecision:
        total = await self._global_total()
        if total >= self._hard:
            log.error("LLM global hard stop reached", extra={"total_usd": round(total, 2)})
            return BudgetDecision(False, "global_hard_stop", True)
        alert = total >= self._alert
        if alert and not self._alerted:
            self._alerted = True
            log.warning("LLM spend alert threshold crossed", extra={"total_usd": round(total, 2)})
        if user_id is not None:
            day_start = self._clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
            spent = await self._store.user_spend_since(user_id, day_start)
            if spent >= self._user_daily:
                return BudgetDecision(False, "user_daily_cap", alert)
        return BudgetDecision(True, None, alert)
