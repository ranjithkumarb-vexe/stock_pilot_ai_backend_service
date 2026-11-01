"""Shared run-time for the eval checks: DB access, the chat pipeline on the real database, golden-set loaders."""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from app.core.clock import SystemClock
from app.core.config import Settings, get_settings
from app.core.db import Connection, Database, set_database, system_tx, user_tx
from app.core.personas import PERSONAS
from app.modules.chat import service
from app.modules.chat.service import ChatResult
from app.modules.llm.client import LlmClient, client_for_tests
from app.modules.llm.trace import TraceRecorder
from app.modules.paper.context import EngineContext, build_context

DATA = Path(__file__).resolve().parent / "data"


def load_jsonl(name: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (DATA / name).read_text(encoding="utf-8").splitlines() if x.strip()]


@dataclass
class EvalContext:
    settings: Settings
    now: datetime
    llm: LlmClient
    db: Database | None
    user_id: UUID | None  # persona used for portfolio questions (None when the seed is absent)
    engine: EngineContext | None
    pytest_db: str = "stockpilot_test_eval"
    with_pytest: bool = True
    cache: dict[str, ChatResult] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def as_of(self) -> date:
        return self.now.date()

    def conn(self) -> AbstractAsyncContextManager[Connection]:
        return user_tx(self.user_id) if self.user_id else system_tx()

    def system(self) -> AbstractAsyncContextManager[Connection]:
        return system_tx()

    async def ask(self, message: str) -> ChatResult:
        """One question through the real pipeline (mock/degraded LLM unless a provider key is configured)."""
        if message not in self.cache:
            rec = TraceRecorder("chat:eval", user_id=self.user_id, clock=SystemClock())  # never flushed
            async with self.conn() as conn:
                self.cache[message] = await service.respond(
                    conn,
                    message,
                    rec,
                    self.llm,
                    user_id=self.user_id,
                    ctx=self.engine,
                    price_source=None,
                    as_of=self.as_of,
                )
        return self.cache[message]


async def open_context(*, with_pytest: bool = True, now: datetime | None = None) -> EvalContext:
    settings = get_settings()
    db = Database(settings)
    await db.open()
    set_database(db)
    clock = SystemClock()
    llm = client_for_tests(settings, clock=clock)
    user_id: UUID | None = PERSONAS["priya"].user_id
    engine: EngineContext | None = None
    try:
        async with system_tx() as conn:
            has = await conn.fetchval(
                "select exists (select 1 from portfolios p where p.user_id = $1 and exists "
                "(select 1 from positions s where s.user_id = $1))",
                user_id,
            )
    except Exception:
        has = False
    if has:
        engine = build_context(SimpleNamespace(clock=clock, settings=settings))
    else:
        user_id = None
    return EvalContext(settings, now or datetime.now(UTC), llm, db, user_id, engine, with_pytest=with_pytest)


async def close_context(ctx: EvalContext) -> None:
    if ctx.db is not None:
        await ctx.db.close()
        set_database(None)
