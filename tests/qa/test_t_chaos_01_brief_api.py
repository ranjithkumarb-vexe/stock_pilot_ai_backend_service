"""QA-01 / T-CHAOS-01 at the HTTP level: the Meta model returns 5xx/429 for a stretch of time while a user reads briefs.

SPEC 12.4: "Retries with jitter; cached brief + 'stale' banner; no crash" (NFR-08). The unit tests in tests/llm and
tests/research prove the retry loop and the safe-template fallback in isolation; these tests drive `GET /v1/briefs/{symbol}`
and `POST /v1/briefs/{symbol}/generate` end to end with a failing provider.

Two tests guard the QA-D2 fix (docs/qa/SCENARIO_MATRIX.md): an outage never replaces a good cached brief, and a
degraded brief is retried on the next read once the model is back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.core.clock import FakeClock
from app.modules.llm.client import client_for_tests
from app.modules.llm.providers.mock import MockProvider
from app.modules.llm.settings import LlmSettings
from app.modules.llm.types import ProviderError
from app.modules.research.brief import templates
from app.modules.research.brief.style import CONSERVATIVE_ORDER, StyleCard
from tests.research.conftest import clean_golden, seed_golden

assert templates.NOT_ENOUGH  # importing the module registers the mock "brief_claims" handler

PRIYA = StyleCard(order=CONSERVATIVE_ORDER, jargon=1, depth="standard")
SYMBOL = "AAPL"


def _down(n: int = 200) -> MockProvider:
    """A provider that fails every call for the whole request (a 2-minute outage covers every retry)."""
    return MockProvider(script=[ProviderError("HTTP 503", status=503, retriable=True)] * n)


def _rate_limited(n: int = 200) -> MockProvider:
    return MockProvider(script=[ProviderError("HTTP 429", status=429, retriable=True, retry_after_s=2.0)] * n)


@pytest.fixture
async def golden(db_admin: Any) -> AsyncIterator[Any]:
    await seed_golden(db_admin)
    yield db_admin
    await clean_golden(db_admin)


@pytest.fixture
async def reader(golden: Any, make_user: Any) -> UUID:
    uid: UUID = await make_user()
    await golden.execute(
        "insert into user_features (user_id, key, value, confidence) values ($1, 'style_card', $2, 0.9)",
        uid,
        PRIYA.model_dump(),
    )
    return uid


class Harness:
    def __init__(self, client: httpx.AsyncClient, settings: Any, clock: FakeClock, headers: dict[str, str]):
        self.client, self.settings, self.clock, self.headers = client, settings, clock, headers

    def use(self, provider: MockProvider) -> MockProvider:
        self.client.app.state.llm_client = client_for_tests(  # type: ignore[attr-defined]
            self.settings, provider, clock=self.clock, llm_settings=LlmSettings()
        )
        return provider

    async def get(self) -> httpx.Response:
        return await self.client.get(f"/v1/briefs/{SYMBOL}", headers=self.headers)

    async def regenerate(self) -> httpx.Response:
        return await self.client.post(f"/v1/briefs/{SYMBOL}/generate", headers=self.headers)


@pytest.fixture
async def h(
    reader: UUID, app_factory: Callable[..., Any], auth_headers: Callable[..., dict[str, str]], settings: Any
) -> AsyncIterator[Harness]:
    clock = FakeClock(datetime.now(UTC))
    async with app_factory(clock=clock) as client:
        yield Harness(client, settings, clock, auth_headers(reader))


@pytest.mark.parametrize("make_outage", [_down, _rate_limited], ids=["5xx", "429"])
async def test_brief_endpoints_survive_an_llm_outage_without_a_5xx(
    h: Harness, make_outage: Callable[[], MockProvider]
) -> None:
    """No crash, bounded retries, numbers still verified: the user gets a 200 `degraded` brief built from the ledger."""
    provider = h.use(make_outage())
    first = await h.get()
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "degraded"
    assert body["citations_summary"]["unverified_numbers"] == 0
    assert body["ledger"] and body["disclaimer"]
    assert 1 <= len(provider.calls) <= 60, "retries must be bounded, not an unbounded hammer"
    again = await h.regenerate()
    assert again.status_code == 200, again.text
    assert (
        again.json()["status"] == "degraded" and again.json()["citations_summary"]["unverified_numbers"] == 0
    )
    assert (await h.get()).status_code == 200


async def test_healthy_cached_brief_is_served_without_calling_the_model(h: Harness) -> None:
    """The cache is what keeps the dashboard alive during an outage: a cache hit never reaches the provider."""
    ok = (await h.get()).json()
    assert ok["status"] == "ok"
    provider = h.use(_down())
    served = await h.get()
    assert served.status_code == 200 and served.json()["id"] == ok["id"] and served.json()["status"] == "ok"
    assert provider.calls == []


async def test_regenerate_during_an_outage_keeps_the_last_good_brief(h: Harness) -> None:
    good = (await h.get()).json()
    assert good["status"] == "ok"
    h.use(_down())
    await h.regenerate()
    after = (await h.get()).json()
    assert after["status"] == "ok" or after["stale"] is True, (
        f"the good brief {good['id']} was replaced by a {after['status']} one (stale={after['stale']})"
    )


async def test_a_degraded_brief_is_replaced_once_the_model_is_back(h: Harness) -> None:
    h.use(_down())
    assert (await h.get()).json()["status"] == "degraded"
    h.use(MockProvider())  # the outage is over
    assert (await h.get()).json()["status"] == "ok"
