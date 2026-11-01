"""The in-process alert monitor (ADR-004: APScheduler, no Redis/Celery).

One cycle: decide whether the market calendar wants a poll (T-DATA-03: holidays and weekends are skipped, so a
closed market never produces "no data" noise) -> poll the 8-K sources once for the union of all users' symbols ->
evaluate every user in that user's RLS transaction -> publish new alerts over SSE AFTER the commit.

Cadence (SPEC 7.7: poll every 60-120 s in market hours, EDGAR fair access respected): `ALERTS_POLL_INTERVAL_S`
(default 90, clamped to 30-600). Regular session: every tick. Extended hours: every 5th tick. Overnight on a
trading day: every 10th. Holiday / weekend: never. `ALERTS_AUTOSTART=0` disables the scheduler (tests invoke
`run_cycle` directly).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.clock import Clock
from app.core.db import get_db, user_tx
from app.core.logging import get_logger
from app.modules.alerts.detectors import FilingSignal
from app.modules.alerts.earnings import EarningsSource, FilingHistoryEarnings
from app.modules.alerts.filings import FilingSource
from app.modules.alerts.service import EvalContext, EvalResult, evaluate_user
from app.modules.marketdata.calendar import NyseCalendar
from app.modules.paper.context import build_context

log = get_logger("app.alerts.monitor")

DEFAULT_INTERVAL_S = 90
EXTENDED_EVERY = 5
OVERNIGHT_EVERY = 10
FILING_LOOKBACK = timedelta(hours=72)


def env_flag(name: str, default: bool = True) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() not in {"0", "false", "off", "no"}


def interval_from_env() -> int:
    try:
        return max(30, min(600, int(os.environ.get("ALERTS_POLL_INTERVAL_S", DEFAULT_INTERVAL_S))))
    except ValueError:
        return DEFAULT_INTERVAL_S


@dataclass(slots=True)
class CycleReport:
    ran: bool
    reason: str
    users: int = 0
    created: int = 0
    resolved: int = 0
    signals: int = 0
    results: dict[UUID, EvalResult] = field(default_factory=dict)


class AlertMonitor:
    def __init__(
        self,
        app_state: Any,
        *,
        sources: list[FilingSource],
        earnings: EarningsSource | None = None,
        interval_s: int = DEFAULT_INTERVAL_S,
        calendar: NyseCalendar | None = None,
        lookback: timedelta = FILING_LOOKBACK,
    ) -> None:
        self._state = app_state
        self.sources = sources
        self.earnings: EarningsSource | None = earnings if earnings is not None else FilingHistoryEarnings()
        self.interval_s = interval_s
        self.calendar = calendar or NyseCalendar()
        self.lookback = lookback
        self.tick = 0
        self._scheduler: AsyncIOScheduler | None = None
        self._lock = asyncio.Lock()

    @property
    def clock(self) -> Clock:
        clock: Clock = self._state.clock
        return clock

    # -- calendar gate ---------------------------------------------------
    def poll_decision(self, now: datetime) -> tuple[bool, str]:
        """(should_run, reason) for this tick."""
        svc = getattr(self._state, "market_service", None)
        override = getattr(self._state, "market_override", None)
        if isinstance(override, bool):
            session = "regular" if override else "closed"
            trading_day = True
        else:
            session = svc.session_now() if svc is not None else self.calendar.session_at(now)
            trading_day = self.calendar.trading_day(self.calendar.exchange_date(now)) is not None
        if session == "regular":
            return True, "regular_session"
        if not trading_day:
            return False, "market_holiday_or_weekend"
        n = EXTENDED_EVERY if session in ("pre", "post") else OVERNIGHT_EVERY
        return (self.tick % n == 0), f"{session}_throttled"

    # -- one cycle -----------------------------------------------------------
    async def collect(self, symbols: list[str], now: datetime) -> list[FilingSignal]:
        since = now - self.lookback
        out: list[FilingSignal] = []
        for src in self.sources:
            try:
                out.extend(await src.poll(symbols, since))
            except Exception as exc:  # one failing source must not stop the others
                log.warning("filing source failed", extra={"source": src.name, "error": type(exc).__name__})
        return out

    async def _job_symbols(self) -> list[str]:
        async with get_db().pool.acquire() as conn:
            return sorted(str(r[0]) for r in await conn.fetch("select sp_alert_job_symbols()"))

    async def _job_users(self) -> list[UUID]:
        async with get_db().pool.acquire() as conn:
            return [r[0] for r in await conn.fetch("select sp_alert_job_user_ids()")]

    def deliver(self, user_id: UUID, view: dict[str, Any]) -> None:
        hub = getattr(self._state, "stream_hub", None)
        if hub is not None:
            hub.publish_user_event(user_id, "alert", view)

    def eval_context(self, now: datetime, signals: list[FilingSignal]) -> EvalContext:
        svc = getattr(self._state, "market_service", None)
        return EvalContext(
            now=now,
            ctx=build_context(self._state),
            signals=signals,
            earnings=self.earnings,
            move_allowed=svc.move_alerts_allowed
            if svc is not None and hasattr(svc, "move_alerts_allowed")
            else None,
        )

    async def run_cycle(self, now: datetime | None = None, *, force: bool = False) -> CycleReport:
        async with self._lock:
            now = now or self.clock.now()
            self.tick += 1
            go, reason = (True, "forced") if force else self.poll_decision(now)
            if not go:
                log.info("alert cycle skipped", extra={"reason": reason})
                return CycleReport(False, reason)
            try:
                users, symbols = await self._job_users(), await self._job_symbols()
            except (asyncpg.PostgresError, OSError, RuntimeError, TimeoutError) as exc:
                log.warning("alert cycle could not list users", extra={"error": type(exc).__name__})
                return CycleReport(False, "db_unavailable")
            signals = await self.collect(symbols, now)
            report = CycleReport(True, reason, users=len(users), signals=len(signals))
            ec = self.eval_context(now, signals)
            for uid in users:
                try:
                    async with user_tx(uid) as conn:
                        res = await evaluate_user(conn, uid, ec)
                except (asyncpg.PostgresError, OSError, RuntimeError, TimeoutError) as exc:
                    log.warning("alert evaluation failed", extra={"error": type(exc).__name__})
                    continue
                report.results[uid] = res
                report.created += len(res.created)
                report.resolved += res.resolved
                for view in res.created:  # after the commit, so a client that refetches sees the row
                    self.deliver(uid, view)
            log.info(
                "alert cycle",
                extra={"users": report.users, "signals": report.signals, "new_alerts": report.created},
            )
            return report

    # -- scheduler ---------------------------------------------------------------
    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(
            self._safe_cycle, "interval", seconds=self.interval_s, id="alert_monitor",
            max_instances=1, coalesce=True, misfire_grace_time=60,
        )  # fmt: skip
        self._scheduler.start()

    async def _safe_cycle(self) -> None:
        try:
            await self.run_cycle()
        except Exception as exc:
            log.error("alert cycle crashed", extra={"error": type(exc).__name__})

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
