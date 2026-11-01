"""The eval checks run against the test database (synthetic company ZZKO + seeded Priya): they must produce honest
numbers - failures where the data cannot support a claim, never a silent pass."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio

from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import Database, set_database
from app.core.personas import PERSONAS
from app.modules.llm.client import client_for_tests
from app.modules.paper.context import build_context
from evals import checks
from evals.framework import run_all
from evals.runtime import EvalContext


@pytest_asyncio.fixture
async def ectx(
    settings: Settings, seed_now: datetime, company: str, db_admin: Any
) -> AsyncIterator[EvalContext]:
    from types import SimpleNamespace

    db = Database(settings)
    await db.open()
    set_database(db)
    clock = SystemClock()
    checks._BRIEF_CACHE.clear()
    yield EvalContext(
        settings, datetime.now(UTC), client_for_tests(settings, clock=clock), db, PERSONAS["priya"].user_id,
        build_context(SimpleNamespace(clock=clock, settings=settings)), with_pytest=False,
    )  # fmt: skip
    checks._BRIEF_CACHE.clear()
    await db.close()
    set_database(None)


async def test_full_spec_run_has_real_numbers_and_never_a_silent_pass(ectx: EvalContext) -> None:
    res = await run_all(checks.build_specs(), ectx, extra_not_measured=checks.STATIC_NOT_MEASURED)
    by = {i["id"]: i for i in res["items"]}
    # the test database holds one company, not the 5 golden tickers: the golden numeric set CANNOT pass here
    assert by["T-RAG-01"]["status"] == "fail" and by["T-RAG-01"]["detail"]["values"]["match_rate"] < 1
    for ok in ("T-RAG-05", "T-SEC-01", "T-SEC-02", "T-SEC-05", "T-SEC-06", "T-SEC-07", "T-PERS-02", "T-PERS-03", "T-PERS-05", "T-PERS-07"):  # fmt: skip
        assert by[ok]["status"] == "pass", (ok, by[ok]["actual"])
    assert by["T-RAG-06"]["status"] == "pass" and "searches" in by["T-RAG-06"]["actual"]
    nm = {n["id"] for n in res["not_measured"]}
    assert "T-RAG-03" in nm  # no golden-ticker XBRL here -> not measured, not a pass
    assert {
        f"T-{x}" for x in ("RISK-01", "PAPER-02", "SEC-03")
    } <= nm  # pytest-backed checks skipped (with_pytest=False)
    assert res["thresholds_met"] is False  # honest: T-RAG-01 fails on this database


async def test_sample_injection_traces_are_written_once(ectx: EvalContext, db_admin: Any) -> None:
    await db_admin.execute("delete from traces where trace_id like 'eval-injection:%'")
    first = await checks.injection(ectx)
    second = await checks.injection(ectx)
    assert first.detail["sample_traces_written"] == 3 and second.detail["sample_traces_written"] == 0
    await db_admin.execute("delete from traces where trace_id like 'eval-injection:%'")
