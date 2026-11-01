"""Background feature jobs: an hourly `recompute_features` sweep (APScheduler) plus a debounced pass for users
whose events just arrived. In-process (ADR-004); both are safe to run concurrently (idempotent upserts and a
per-user lock)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from uuid import UUID

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.clock import Clock
from app.core.db import get_db
from app.core.logging import get_logger
from app.modules.personalization.recompute import recompute_features

log = get_logger("app.personalization.scheduler")

SWEEP_LOOKBACK = timedelta(hours=2)
MAX_PER_PASS = 50


class FeatureScheduler:
    def __init__(self, clock: Clock, *, interval_minutes: int = 60, debounce_s: float = 20.0) -> None:
        self.clock = clock
        self.interval_minutes = interval_minutes
        self.debounce_s = debounce_s
        self._dirty: set[UUID] = set()
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._scheduler: AsyncIOScheduler | None = None
        self._task: asyncio.Task[None] | None = None

    def mark_dirty(self, user_id: UUID) -> None:
        self._dirty.add(user_id)

    @property
    def pending(self) -> int:
        return len(self._dirty)

    async def recompute_user(self, user_id: UUID) -> bool:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            try:
                await recompute_features(user_id, now=self.clock.now())
            except (asyncpg.PostgresError, OSError, RuntimeError, TimeoutError) as exc:
                log.warning("recompute failed", extra={"error": type(exc).__name__})
                return False
            finally:
                if len(self._locks) > 1000:
                    self._locks.clear()
        return True

    async def run_dirty_once(self) -> int:
        batch = list(self._dirty)[:MAX_PER_PASS]
        self._dirty.difference_update(batch)
        done = 0
        for uid in batch:
            done += await self.recompute_user(uid)
        return done

    async def run_sweep_once(self) -> int:
        """The hourly job: users with recent events or open positions (ids via a SECURITY DEFINER function)."""
        since = self.clock.now() - SWEEP_LOOKBACK
        try:
            async with get_db().pool.acquire() as conn:
                ids = [r[0] for r in await conn.fetch("select sp_feature_job_user_ids($1)", since)]
        except (asyncpg.PostgresError, OSError, RuntimeError, TimeoutError) as exc:
            log.warning("sweep could not list users", extra={"error": type(exc).__name__})
            return 0
        done = 0
        for uid in ids:
            done += await self.recompute_user(uid)
        log.info("hourly feature sweep", extra={"users": len(ids), "ok": done})
        return done

    async def _debounce_loop(self) -> None:
        while True:
            await asyncio.sleep(self.debounce_s)
            try:
                await self.run_dirty_once()
            except Exception as exc:
                log.error("dirty pass failed", extra={"error": type(exc).__name__})

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(
            self.run_sweep_once, "interval", minutes=self.interval_minutes, id="recompute_features",
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )  # fmt: skip
        self._scheduler.start()
        self._task = asyncio.create_task(self._debounce_loop(), name="features-debounce")

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
