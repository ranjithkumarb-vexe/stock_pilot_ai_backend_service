"""The single entry point for every LLM call (SPEC 7.3).

    resp = await client.call(LlmRequest(job="brief_claims", messages=[...], data_class="user",
                                        user_id=uid, response_model=BriefClaims))
    if resp.degraded: ...   # provider outage / budget stop / invalid output: use the safe template

Guarantees
* Policy: raises `PolicyViolationError` if the job's route is Contributor mode and `data_class != "public"`
  (T-SEC-06). Reasoning effort "none" is unrepresentable.
* Budgets: per-user daily cap and global alert/hard-stop from `llm_calls` rows (hard stop => degraded response).
* Resilience (T-CHAOS-01): retriable failures back off exponentially with jitter and honour `Retry-After`;
  exhausted retries return `degraded=True` - never an exception into the request path.
* Structured output: JSON-schema from a Pydantic model, validated; one repair retry, then degraded.
* Accounting: one `llm_calls` row per attempt-chain, trace step + totals on the active `TraceRecorder`.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.logging import get_logger, trace_id_var
from app.modules.llm.budget import BudgetGuard, InMemoryUsageStore, PgUsageStore, UsageRecord, UsageStore
from app.modules.llm.providers.base import Provider
from app.modules.llm.providers.meta import MetaProvider
from app.modules.llm.providers.mock import MockProvider
from app.modules.llm.routing import model_for, route_for
from app.modules.llm.settings import LlmSettings, get_llm_settings
from app.modules.llm.trace import TraceRecorder, current_trace
from app.modules.llm.types import (
    LlmRequest,
    LlmResponse,
    Message,
    PolicyViolationError,
    ProviderError,
    ProviderRequest,
    ProviderResult,
)

log = get_logger("app.llm.client")
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def enforce_policy(mode: str, data_class: str) -> None:
    """T-SEC-06: Contributor mode may only ever see public data."""
    if mode == "contributor" and data_class != "public":
        raise PolicyViolationError("Contributor mode is restricted to public data (data_class='public')")


def parse_structured(text: str, model: type[BaseModel]) -> BaseModel:
    m = _FENCE.match(text)
    return model.model_validate_json(m.group(1) if m else text)


def _error_summary(exc: ValidationError | ValueError) -> str:
    if isinstance(exc, ValidationError):  # locations and types only: never echo model output
        return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()[:8])
    return "not valid JSON"


class LlmClient:
    def __init__(
        self,
        settings: Settings,
        provider: Provider,
        store: UsageStore,
        clock: Clock | None = None,
        *,
        llm_settings: LlmSettings | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm_settings or get_llm_settings()
        self.provider = provider
        self.store = store
        self.clock = clock or SystemClock()
        self._rng = rng or random.Random()  # noqa: S311 - jitter
        self.budget = BudgetGuard(
            store,
            self.clock,
            user_daily_usd=self.llm.llm_user_daily_budget_usd,
            alert_usd=settings.llm_budget_alert_usd,
            hard_stop_usd=settings.llm_budget_hard_stop_usd,
        )

    async def aclose(self) -> None:
        await self.provider.aclose()

    # ------------------------------------------------------------------ public
    async def call(self, req: LlmRequest, *, trace: TraceRecorder | None = None) -> LlmResponse:
        route = route_for(req.job)
        enforce_policy(route.mode, req.data_class)
        if req.data_class not in route.allowed_data:
            raise PolicyViolationError(f"job {req.job!r} may not process data_class={req.data_class!r}")
        effort = req.effort or route.effort or self.llm.llm_reasoning_effort
        model = model_for(route, self.settings)
        resp = LlmResponse(model=model, mode=route.mode, provider=self.provider.name)
        rec = trace or current_trace()
        t0 = time.perf_counter()

        decision = await self.budget.check(req.user_id)
        resp.budget_alert = decision.alert
        if not decision.allowed:
            resp.degraded, resp.degraded_reason = True, f"budget:{decision.reason}"
            await self._finish(req, resp, rec, t0, outcome=f"budget:{decision.reason}")
            return resp

        schema = req.response_model.model_json_schema() if req.response_model else None
        messages = list(req.messages)
        for repair in (False, True):
            preq = ProviderRequest(
                job=req.job, model=model, mode=route.mode, messages=messages, effort=effort,
                max_tokens=req.max_tokens, temperature=req.temperature, json_schema=schema, hints=req.hints,
            )  # fmt: skip
            result = await self._attempt_chain(preq, resp)
            if result is None:
                resp.degraded = True
                await self._finish(req, resp, rec, t0, outcome=f"degraded:{resp.degraded_reason}")
                return resp
            resp.text = result.text
            resp.model = result.model
            resp.tokens_in += result.tokens_in
            resp.tokens_out += result.tokens_out
            if req.response_model is None:
                break
            try:
                resp.parsed = parse_structured(result.text, req.response_model)
                break
            except (ValidationError, ValueError) as exc:
                if repair:
                    resp.degraded, resp.degraded_reason, resp.parsed = True, "invalid_structured_output", None
                    await self._finish(req, resp, rec, t0, outcome="degraded:invalid_structured_output")
                    return resp
                messages = [
                    *messages,
                    Message("assistant", result.text[:4000]),
                    Message(
                        "user",
                        "Your previous output failed schema validation ("
                        + _error_summary(exc)
                        + "). Return ONLY valid JSON that matches the schema.",
                    ),
                ]
        await self._finish(req, resp, rec, t0, outcome="ok")
        return resp

    # ------------------------------------------------------------------ internals
    def _delay(self, attempt: int, retry_after: float | None) -> float:
        base = min(self.llm.llm_backoff_cap_s, self.llm.llm_backoff_base_s * (2**attempt))
        delay = float(base) * (0.5 + self._rng.random() * 0.5)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.llm.llm_backoff_cap_s))
        return delay

    async def _attempt_chain(self, preq: ProviderRequest, resp: LlmResponse) -> ProviderResult | None:
        for attempt in range(self.llm.llm_max_retries + 1):
            resp.attempts += 1
            try:
                async with asyncio.timeout(self.llm.llm_timeout_s):
                    return await self.provider.complete(preq)
            except TimeoutError:
                err = ProviderError("timeout", retriable=True)
            except ProviderError as exc:
                err = exc
            if not err.retriable:
                resp.degraded_reason = f"provider_error:{err.status or 'fatal'}"
                return None
            resp.degraded_reason = f"provider_unavailable:{err.status or 'network'}"
            if attempt >= self.llm.llm_max_retries:
                return None
            delay = self._delay(attempt, err.retry_after_s)
            log.warning("llm retry", extra={"job": preq.job, "attempt": attempt + 1, "delay_s": round(delay, 2),
                                            "status": err.status})  # fmt: skip
            await self.clock.sleep(delay)
        return None

    def _cost(self, tin: int, tout: int) -> float:
        return round(
            tin / 1e6 * self.llm.llm_price_in_per_mtok + tout / 1e6 * self.llm.llm_price_out_per_mtok, 6
        )

    async def _finish(
        self, req: LlmRequest, resp: LlmResponse, rec: TraceRecorder | None, t0: float, *, outcome: str
    ) -> None:
        resp.latency_ms = int((time.perf_counter() - t0) * 1000)
        resp.cost_usd = self._cost(resp.tokens_in, resp.tokens_out)
        if resp.degraded_reason and not outcome.startswith(("degraded", "budget")):
            resp.degraded_reason = None
        try:
            await self.store.record(
                UsageRecord(
                    user_id=req.user_id,
                    trace_id=trace_id_var.get(),
                    job=req.job,
                    model=resp.model,
                    mode=resp.mode,
                    data_class=req.data_class,
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                    cost_usd=resp.cost_usd,
                    latency_ms=resp.latency_ms,
                    outcome=outcome,
                    attempts=max(1, resp.attempts),
                    provider=resp.provider,
                    prompt_version=req.prompt_version,
                )
            )
            self.budget.invalidate()
        except Exception:
            log.exception("llm usage record failed")
        if rec is not None:
            rec.add_usage(model=resp.model, mode=resp.mode, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                          cost_usd=resp.cost_usd)  # fmt: skip
            rec.add(
                f"llm:{req.job}",
                resp.latency_ms,
                "ok" if outcome == "ok" else "error",
                {"model": resp.model, "mode": resp.mode, "attempts": resp.attempts, "tokens_in": resp.tokens_in,
                 "tokens_out": resp.tokens_out, "prompt_version": req.prompt_version, "degraded_reason": resp.degraded_reason},
            )  # fmt: skip


def build_client(
    settings: Settings,
    clock: Clock | None = None,
    *,
    store: UsageStore | None = None,
    provider: Provider | None = None,
) -> LlmClient:
    """Provider from `LLM_PROVIDER` (`mock` | `meta`); `meta` without a key falls back to mock (logged)."""
    llm = get_llm_settings()
    if provider is None:
        if settings.llm_provider == "meta" and settings.meta_model_api_key:
            provider = MetaProvider(
                settings.meta_model_api_key, settings.meta_api_base_url, timeout_s=llm.llm_timeout_s
            )
        else:
            if settings.llm_provider == "meta":
                log.warning(
                    "LLM_PROVIDER=meta but META_MODEL_API_KEY is empty: using MockProvider (degraded mode)"
                )
            provider = MockProvider()
    return LlmClient(settings, provider, store or PgUsageStore(), clock, llm_settings=llm)


def client_for_tests(settings: Settings, provider: Provider | None = None, **kw: Any) -> LlmClient:
    store = kw.pop("store", None) or InMemoryUsageStore()
    return LlmClient(settings, provider or MockProvider(), store, **kw)


__all__ = ["LlmClient", "build_client", "client_for_tests", "enforce_policy", "parse_structured"]
