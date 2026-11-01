"""BE-13: policy (T-SEC-06), retries/degrade (T-CHAOS-01), structured output, budgets, accounting."""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel

from app.core.clock import FakeClock
from app.core.config import Settings
from app.modules.llm.budget import InMemoryUsageStore, PgUsageStore
from app.modules.llm.client import LlmClient, build_client, client_for_tests, enforce_policy, parse_structured
from app.modules.llm.prompts import load_prompt
from app.modules.llm.providers.meta import MetaProvider
from app.modules.llm.providers.mock import MockProvider, register_mock_handler
from app.modules.llm.routing import ROUTES, model_for, route_for
from app.modules.llm.settings import LlmSettings
from app.modules.llm.trace import TraceRecorder, trace_scope
from app.modules.llm.types import LlmRequest, Message, PolicyViolationError, ProviderError, ProviderRequest

U = Message("user", "hello")


class Out(BaseModel):
    answer: str
    n: int = 0


def llm_settings(**kw: Any) -> LlmSettings:
    base: dict[str, Any] = {"llm_backoff_base_s": 1.0, "llm_max_retries": 3, "llm_timeout_s": 5.0}
    return LlmSettings.model_construct(**{**LlmSettings().model_dump(), **base, **kw})


def make(
    settings: Settings, provider: MockProvider | None = None, clock: FakeClock | None = None, **kw: Any
) -> LlmClient:
    return client_for_tests(
        settings,
        provider,
        clock=clock or FakeClock(),
        llm_settings=kw.pop("llm_settings", llm_settings()),
        rng=random.Random(7),
        **kw,
    )


def e(status: int, retry_after: float | None = None) -> ProviderError:
    return ProviderError(f"HTTP {status}", status=status, retriable=True, retry_after_s=retry_after)


# ------------------------------------------------------------------ routing / policy
def test_routing_table_matches_spec_7_3(settings: Settings) -> None:
    for job in ("summarise_10k", "extract_risks", "classify_news", "rerank_chunks"):
        r = route_for(job)
        assert (r.mode, r.allowed_data, r.effort) == ("contributor", ("public",), "low")
        assert model_for(r, settings) == settings.model_contributor
    for job in ("brief_claims", "chat_qa", "proposal_wording", "topic_tagging"):
        r = route_for(job)
        assert r.mode == "standard" and "user" in r.allowed_data
        assert model_for(r, settings) == settings.model_standard
    assert model_for(route_for("weekly_deep_dive"), settings) == settings.model_deep
    assert all(r.effort in {"minimal", "low", "medium", "high"} for r in ROUTES.values())  # never "none"
    with pytest.raises(KeyError):
        route_for("nope")


def test_policy_contributor_requires_public() -> None:
    enforce_policy("contributor", "public")
    enforce_policy("standard", "user")
    with pytest.raises(PolicyViolationError):
        enforce_policy("contributor", "user")


async def test_t_sec_06_call_raises_for_user_data_in_contributor_mode(settings: Settings) -> None:
    provider = MockProvider()
    client = make(settings, provider)
    with pytest.raises(PolicyViolationError):
        await client.call(LlmRequest(job="summarise_10k", messages=[U], data_class="user"))
    with pytest.raises(
        PolicyViolationError
    ):  # also: public-only route given a standard-mode job w/ bad class
        await client.call(LlmRequest(job="rerank_chunks", messages=[U], data_class="user", user_id=uuid4()))
    assert provider.calls == []  # nothing was sent
    ok = await client.call(LlmRequest(job="summarise_10k", messages=[U], data_class="public"))
    assert ok.ok and ok.mode == "contributor" and ok.model == "mock:" + client.settings.model_contributor


# ------------------------------------------------------------------ chaos / retries
async def test_t_chaos_01_retries_with_jitter_then_succeeds(settings: Settings) -> None:
    clock = FakeClock()
    provider = MockProvider(script=[e(503), e(429), e(500), None])
    client = make(settings, provider, clock)
    t0 = clock.monotonic()
    resp = await client.call(LlmRequest(job="chat_qa", messages=[U], data_class="user"))
    assert resp.ok and resp.attempts == 4 and len(provider.calls) == 4
    waited = clock.monotonic() - t0
    # equal jitter: sum of delays within [0.5, 1.0] * (1 + 2 + 4)
    assert 3.5 <= waited <= 7.0


async def test_retry_after_is_honoured(settings: Settings) -> None:
    clock = FakeClock()
    client = make(settings, MockProvider(script=[e(429, retry_after=9.0), None]), clock)
    t0 = clock.monotonic()
    assert (await client.call(LlmRequest(job="chat_qa", messages=[U], data_class="user"))).ok
    assert clock.monotonic() - t0 >= 9.0


async def test_outage_for_minutes_degrades_without_raising(settings: Settings) -> None:
    provider = MockProvider(script=[e(503)] * 20)
    store = InMemoryUsageStore()
    client = make(settings, provider, store=store)
    resp = await client.call(LlmRequest(job="brief_claims", messages=[U], data_class="user", user_id=uuid4()))
    assert resp.degraded and resp.degraded_reason == "provider_unavailable:503"
    assert resp.attempts == 4 and resp.text == "" and resp.parsed is None
    assert store.records[-1][1].outcome.startswith("degraded")  # failure is accounted for


async def test_non_retriable_error_degrades_immediately(settings: Settings) -> None:
    provider = MockProvider(script=[ProviderError("HTTP 401", status=401, retriable=False)])
    resp = await make(settings, provider).call(LlmRequest(job="chat_qa", messages=[U], data_class="user"))
    assert resp.degraded and resp.attempts == 1 and resp.degraded_reason == "provider_error:401"


async def test_timeout_is_retriable(settings: Settings) -> None:
    import asyncio

    class Slow(MockProvider):
        async def complete(self, req: ProviderRequest) -> Any:
            if not self.calls:
                self.calls.append(req)
                await asyncio.sleep(1)
            return await super().complete(req)

    resp = await make(settings, Slow(), llm_settings=llm_settings(llm_timeout_s=0.05)).call(
        LlmRequest(job="chat_qa", messages=[U], data_class="user")
    )
    assert resp.ok and resp.attempts == 2


# ------------------------------------------------------------------ structured output
async def test_structured_output_valid(settings: Settings) -> None:
    provider = MockProvider(script=['```json\n{"answer": "hi", "n": 3}\n```'])
    resp = await make(settings, provider).call(
        LlmRequest(job="chat_qa", messages=[U], data_class="user", response_model=Out)
    )
    assert isinstance(resp.parsed, Out) and resp.parsed.n == 3
    assert (
        provider.calls[0].json_schema is not None and "answer" in provider.calls[0].json_schema["properties"]
    )


async def test_invalid_output_gets_one_repair_retry_then_succeeds(settings: Settings) -> None:
    provider = MockProvider(script=['{"wrong": 1}', '{"answer": "fixed"}'])
    resp = await make(settings, provider).call(
        LlmRequest(job="chat_qa", messages=[U], data_class="user", response_model=Out)
    )
    assert resp.ok and isinstance(resp.parsed, Out) and resp.parsed.answer == "fixed"
    assert len(provider.calls) == 2
    repair = provider.calls[1].messages[-1].content
    assert "answer: missing" in repair and "wrong" not in repair  # error summary never echoes output


async def test_invalid_output_twice_degrades(settings: Settings) -> None:
    provider = MockProvider(script=["not json", '{"answer": 5, "n": "x"}'])
    resp = await make(settings, provider).call(
        LlmRequest(job="chat_qa", messages=[U], data_class="user", response_model=Out)
    )
    assert resp.degraded and resp.degraded_reason == "invalid_structured_output" and resp.parsed is None
    assert len(provider.calls) == 2


def test_parse_structured_strips_fences() -> None:
    assert parse_structured('```\n{"answer":"a"}\n```', Out).model_dump() == {"answer": "a", "n": 0}


# ------------------------------------------------------------------ budgets
async def test_per_user_daily_budget_stops_calls_but_not_other_users(settings: Settings) -> None:
    clock = FakeClock()
    ls = llm_settings(
        llm_user_daily_budget_usd=0.10, llm_price_in_per_mtok=1_000_000, llm_price_out_per_mtok=1_000_000
    )
    provider = MockProvider()
    big = settings.model_copy(update={"llm_budget_alert_usd": 1e12, "llm_budget_hard_stop_usd": 1e13})
    client = make(big, provider, clock, llm_settings=ls, store=InMemoryUsageStore(clock))
    a, b = uuid4(), uuid4()

    def req(u: UUID) -> LlmRequest:
        return LlmRequest(
            job="chat_qa", messages=[Message("user", "word " * 50)], data_class="user", user_id=u
        )

    first = await client.call(req(a))
    assert first.ok and first.cost_usd > 0.10
    blocked = await client.call(req(a))
    assert blocked.degraded and blocked.degraded_reason == "budget:user_daily_cap"
    assert len(provider.calls) == 1  # the blocked call never reached the provider
    assert (await client.call(req(b))).ok
    clock.advance(24 * 3600)  # next day: allowed again
    assert (await client.call(req(a))).ok


async def test_global_alert_and_hard_stop(settings: Settings) -> None:
    clock = FakeClock()
    store = InMemoryUsageStore(clock)
    s = settings.model_copy(update={"llm_budget_alert_usd": 100.0, "llm_budget_hard_stop_usd": 140.0})
    ls = llm_settings(
        llm_user_daily_budget_usd=10_000, llm_price_in_per_mtok=1_000_000, llm_price_out_per_mtok=0
    )
    client = make(s, MockProvider(), clock, store=store, llm_settings=ls)
    req = LlmRequest(
        job="chat_qa", messages=[Message("user", "w " * 75)], data_class="user", user_id=uuid4()
    )  # ~100 tok in
    from app.modules.llm.budget import UsageRecord

    await store.record(UsageRecord(None, None, "x", "m", "standard", "public", 0, 0, 105.0, 1, "ok"))
    r = await client.call(req)
    assert r.ok and r.budget_alert  # between alert and stop: still served, flagged
    await store.record(UsageRecord(None, None, "x", "m", "standard", "public", 0, 0, 40.0, 1, "ok"))
    client.budget.invalidate()
    r2 = await client.call(req)
    assert r2.degraded and r2.degraded_reason == "budget:global_hard_stop"


async def test_trace_gets_llm_step_and_totals(settings: Settings) -> None:
    rec = TraceRecorder("unit", clock=FakeClock())
    client = make(settings)
    await client.call(
        LlmRequest(job="chat_qa", messages=[U], data_class="user", prompt_version="p-v1"), trace=rec
    )
    assert rec.steps[0].name == "llm:chat_qa" and rec.steps[0].detail["prompt_version"] == "p-v1"
    assert rec.tokens_in > 0 and rec.model and rec.mode == "standard"


# ------------------------------------------------------------------ Postgres accounting
async def test_pg_usage_store_rows_budget_and_global_function(
    settings: Settings, app_client: Any, make_user: Callable[..., Awaitable[Any]], db_admin: Any
) -> None:
    uid = await make_user()
    other = await make_user()
    ls = llm_settings(
        llm_user_daily_budget_usd=0.0001, llm_price_in_per_mtok=1000, llm_price_out_per_mtok=1000
    )
    client = LlmClient(settings, MockProvider(), PgUsageStore(), FakeClock(), llm_settings=ls)
    # the app fixture opened the pool: a real FakeClock day boundary is irrelevant because rows use now()
    client.budget._clock = __import__("app.core.clock", fromlist=["SystemClock"]).SystemClock()
    r = await client.call(
        LlmRequest(job="chat_qa", messages=[U], data_class="user", user_id=uid, prompt_version="pv")
    )
    assert r.ok
    row = await db_admin.fetchrow("select * from llm_calls where user_id = $1", uid)
    assert row["job"] == "chat_qa" and row["mode"] == "standard" and row["data_class"] == "user"
    assert row["prompt_version"] == "pv" and row["provider"] == "mock" and row["cost_usd"] > 0
    blocked = await client.call(LlmRequest(job="chat_qa", messages=[U], data_class="user", user_id=uid))
    assert blocked.degraded_reason == "budget:user_daily_cap"
    sys_call = await client.call(LlmRequest(job="summarise_10k", messages=[U], data_class="public"))
    assert sys_call.ok
    assert (
        await db_admin.fetchval(
            "select count(*) from llm_calls where user_id is null and job='summarise_10k'"
        )
        >= 1
    )
    total = await PgUsageStore().global_spend_since(
        __import__("datetime").datetime(2000, 1, 1, tzinfo=__import__("datetime").UTC)
    )
    assert total >= float(
        await db_admin.fetchval("select coalesce(sum(cost_usd),0) from llm_calls where user_id = $1", uid)
    )
    assert (
        await PgUsageStore().user_spend_since(
            other, __import__("datetime").datetime(2000, 1, 1, tzinfo=__import__("datetime").UTC)
        )
        == 0
    )
    await db_admin.execute("delete from llm_calls where user_id is null")


async def test_trace_scope_persists_trace_and_steps(
    app_client: Any, make_user: Callable[..., Awaitable[Any]], db_admin: Any
) -> None:
    uid = await make_user()
    async with trace_scope("brief:TEST", user_id=uid) as t:
        with t.step("retrieve", chunks=3):
            pass
        t.add("verify", 4, "ok", {"unverified": 0})
        t.add_usage(model="m", mode="standard", tokens_in=10, tokens_out=5, cost_usd=0.001)
    assert t.row_id is not None
    row = await db_admin.fetchrow("select * from traces where id = $1", t.row_id)
    assert row["name"] == "brief:TEST" and row["tokens_in"] == 10 and row["outcome"] == "ok"
    steps = await db_admin.fetch(
        "select idx, name, detail from trace_steps where trace_ref = $1 order by idx", t.row_id
    )
    assert [s["name"] for s in steps] == ["retrieve", "verify"] and steps[1]["detail"] == {"unverified": 0}
    with pytest.raises(RuntimeError):
        async with trace_scope("boom", user_id=uid) as t2:
            raise RuntimeError("x")
    assert (await db_admin.fetchval("select outcome from traces where id = $1", t2.row_id)) == "error"


# ------------------------------------------------------------------ Meta provider (mocked transport only)
def meta(handler: Callable[[httpx.Request], httpx.Response]) -> MetaProvider:
    return MetaProvider("k-test", "https://api.example.test/v1", transport=httpx.MockTransport(handler))


def preq(**kw: Any) -> ProviderRequest:
    base: dict[str, Any] = {"job": "chat_qa", "model": "muse-spark-1.3", "mode": "standard", "messages": [U], "effort": "low",
                                "max_tokens": 100, "temperature": 0.1, "json_schema": None, "hints": {}}  # fmt: skip
    return ProviderRequest(**{**base, **kw})


async def test_meta_provider_payload_and_parsing() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = (
            str(req.url),
            req.headers["authorization"],
            json.loads(req.content),
        )
        return httpx.Response(200, json={"model": "muse-spark-1.3", "choices": [{"message": {"content": "hi"}}],
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 3}})  # fmt: skip

    p = meta(handler)
    res = await p.complete(preq(json_schema={"type": "object"}))
    assert (res.text, res.tokens_in, res.tokens_out) == ("hi", 11, 3)
    assert seen["url"] == "https://api.example.test/v1/chat/completions" and seen["auth"] == "Bearer k-test"
    assert (
        seen["body"]["reasoning_effort"] == "low" and seen["body"]["response_format"]["type"] == "json_schema"
    )
    await p.aclose()


async def test_meta_provider_never_sends_effort_none_and_maps_errors() -> None:
    with pytest.raises(ValueError, match="effort"):
        MetaProvider.build_payload(preq(effort="none"))
    with pytest.raises(ValueError, match="META_MODEL_API_KEY"):
        MetaProvider("", "https://x")

    def h429(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"})

    with pytest.raises(ProviderError) as ei:
        await meta(h429).complete(preq())
    assert ei.value.retriable and ei.value.retry_after_s == 12.0 and ei.value.status == 429

    with pytest.raises(ProviderError) as e400:
        await meta(lambda r: httpx.Response(400)).complete(preq())
    assert not e400.value.retriable

    with pytest.raises(ProviderError) as ebad:
        await meta(lambda r: httpx.Response(200, json={"oops": 1})).complete(preq())
    assert not ebad.value.retriable

    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    with pytest.raises(ProviderError) as enet:
        await meta(boom).complete(preq())
    assert enet.value.retriable


async def test_build_client_falls_back_to_mock_without_key(settings: Settings) -> None:
    c = build_client(settings.model_copy(update={"llm_provider": "meta", "meta_model_api_key": ""}))
    assert c.provider.name == "mock"
    c2 = build_client(settings.model_copy(update={"llm_provider": "meta", "meta_model_api_key": "k"}))
    assert c2.provider.name == "meta"
    await c2.aclose()


async def test_mock_handlers_and_prompt_versions(settings: Settings) -> None:
    register_mock_handler("topic_tagging", lambda req: json.dumps({"answer": req.hints["x"]}))
    resp = await make(settings).call(
        LlmRequest(
            job="topic_tagging", messages=[U], data_class="user", response_model=Out, hints={"x": "from-hint"}
        )
    )
    assert isinstance(resp.parsed, Out) and resp.parsed.answer == "from-hint"
    p = load_prompt("brief_claims")
    assert p.version == "brief-claims-v1" and "{{m3}}" in p.text
    with pytest.raises(ValueError, match="invalid prompt name"):
        load_prompt("../etc")
