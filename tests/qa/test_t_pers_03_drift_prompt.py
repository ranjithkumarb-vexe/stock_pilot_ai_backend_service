"""QA-01 / T-PERS-03: "Interest drift dividends -> AI. Top interest flips within 20 events; drift prompt shown".

The flip within 20 events is proven in tests/personalization and tests/ranking. The *prompt* ("Has your focus changed?",
SPEC 10.8: weekly KL divergence > 0.3) is computed and stored as `user_features.interest_drift`, but nothing serves it:
neither `GET /v1/profile` nor `GET /v1/feed` carries it, so the web app cannot show it. That is defect QA-D1
(docs/qa/SCENARIO_MATRIX.md). The `xfail(strict=True)` test flips to a loud XPASS when the product exposes the prompt.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from tests.personalization.conftest import World, recompute, world  # noqa: F401  (fixtures)


async def _drifting_user(make_user: Any, world: World, recompute: Callable[..., Awaitable[Any]]) -> UUID:  # noqa: F811
    uid: UUID = await make_user()
    now = datetime.now(UTC)
    await world.profile(uid, 2)
    for i in range(12):  # three weeks ago: Consumer Staples (the "dividends" focus)
        await world.event(uid, "ticker_viewed", now - timedelta(days=10 + i * 0.5), {"ticker": "QAD1"})
    for i in range(12):  # this week: Technology (the "AI" focus)
        await world.event(uid, "ticker_viewed", now - timedelta(hours=2 + i * 6), {"ticker": "QAT1"})
    await recompute(uid, now=now)
    return uid


async def test_interest_drift_flag_is_computed_and_stored_after_a_focus_change(
    make_user: Any,
    world: World,  # noqa: F811
    recompute: Callable[..., Awaitable[Any]],  # noqa: F811
) -> None:
    uid = await _drifting_user(make_user, world, recompute)
    drift = await world.feature(uid, "interest_drift")
    assert drift["flag"] is True and drift["kl"] > 0.3 and drift["enough_data"] is True


@pytest.mark.xfail(
    strict=True,
    reason="QA-D1: the interest-drift prompt is computed but not exposed by GET /v1/profile or GET /v1/feed",
)
async def test_the_drift_prompt_reaches_the_client(
    make_user: Any,
    world: World,  # noqa: F811
    recompute: Callable[..., Awaitable[Any]],  # noqa: F811
    app_factory: Callable[..., Any],
    auth_headers: Callable[..., dict[str, str]],
) -> None:
    uid = await _drifting_user(make_user, world, recompute)
    h = auth_headers(uid)
    async with app_factory() as client:  # after the recompute: the fixture resets the process-wide DB handle
        profile = await client.get("/v1/profile", headers=h)
        feed = await client.get("/v1/feed", headers=h)
    assert profile.status_code == 200, profile.text
    assert feed.status_code == 200, feed.text
    text = (json.dumps(profile.json()) + json.dumps(feed.json())).lower()
    assert "drift" in text or "focus changed" in text, "no response carries the interest-drift prompt"
