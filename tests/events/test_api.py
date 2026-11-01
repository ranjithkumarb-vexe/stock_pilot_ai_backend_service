"""POST /v1/events against the real test DB."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.clock import FakeClock

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


def good(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_id": str(uuid4()),
        "ts": "2026-10-30T14:59:00Z",
        "session_id": "s-1",
        "type": "ticker_viewed",
        "entity": {"ticker": "NVDA"},
        "props": {},
        "context": {"market_session": "regular"},
        "client": {"device": "desktop", "tz": "UTC"},
        "schema_version": 1,
    }
    base.update(over)
    return base


async def test_requires_auth(app_client: Any) -> None:
    r = await app_client.post("/v1/events", json={"events": []})
    assert r.status_code == 401


async def test_accepts_valid_and_reports_index_and_reason_for_invalid(
    app_factory: Callable[..., Any], make_user: Any, auth_headers: Any, db_admin: Any
) -> None:
    uid = await make_user()
    batch = [
        good(),
        good(type="bogus"),
        good(entity={}),
        "junk",
        good(type="proposal_rejected"),
        good(ts="nope"),
    ]
    async with app_factory(clock=FakeClock(NOW)) as c:
        r = await c.post("/v1/events", json={"events": batch}, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected"] == [
        {"index": 1, "reason": "unknown_type"},
        {"index": 2, "reason": "missing_entity.ticker"},
        {"index": 3, "reason": "not_an_object"},
        {"index": 4, "reason": "server_authoritative_type"},
        {"index": 5, "reason": "invalid_ts"},
    ]
    rows = await db_admin.fetch("select type, entity, user_id from events where user_id = $1", uid)
    assert len(rows) == 1 and rows[0]["entity"] == {"ticker": "NVDA"}
    audit = await db_admin.fetchrow(
        "select details from audit_log where user_id = $1 and action = 'events.ingest'", uid
    )
    assert audit["details"]["accepted"] == 1 and audit["details"]["rejected"] == 5


async def test_batch_limit_is_100(app_factory: Callable[..., Any], make_user: Any, auth_headers: Any) -> None:
    uid = await make_user()
    async with app_factory(clock=FakeClock(NOW)) as c:
        h = auth_headers(uid)
        ok = await c.post("/v1/events", json={"events": [good() for _ in range(100)]}, headers=h)
        assert ok.status_code == 200 and ok.json()["accepted"] == 100
        over = await c.post("/v1/events", json={"events": [good() for _ in range(101)]}, headers=h)
        assert over.status_code == 422 and over.json()["code"] == "validation_error"
        assert over.json()["details"]["max"] == 100
        assert (await c.post("/v1/events", json={"events": "x"}, headers=h)).status_code == 422
        assert (await c.post("/v1/events", json={}, headers=h)).status_code == 422
        empty = await c.post("/v1/events", json={"events": []}, headers=h)
        assert empty.json() == {"accepted": 0, "rejected": []}


async def test_idempotent_by_event_id_and_batch_duplicates(
    app_factory: Callable[..., Any], make_user: Any, auth_headers: Any, db_admin: Any
) -> None:
    uid = await make_user()
    e = good()
    async with app_factory(clock=FakeClock(NOW)) as c:
        h = auth_headers(uid)
        r = await c.post("/v1/events", json={"events": [e, e]}, headers=h)
        assert r.json()["accepted"] == 1 and r.json()["rejected"] == [
            {"index": 1, "reason": "duplicate_event_id"}
        ]
        again = await c.post("/v1/events", json={"events": [e]}, headers=h)  # client retry after a timeout
        assert again.status_code == 200
    assert await db_admin.fetchval("select count(*) from events where user_id = $1", uid) == 1


async def test_events_are_stored_under_the_callers_id_only(
    app_factory: Callable[..., Any], make_user: Any, auth_headers: Any, db_admin: Any
) -> None:
    a, b = await make_user(), await make_user()
    shared = str(uuid4())
    async with app_factory(clock=FakeClock(NOW)) as c:
        await c.post("/v1/events", json={"events": [good(event_id=shared)]}, headers=auth_headers(a))
        # B reusing A's event id must neither fail nor touch A's row (PK is (user_id, event_id))
        r = await c.post(
            "/v1/events",
            json={"events": [good(event_id=shared, entity={"ticker": "AAPL"})]},
            headers=auth_headers(b),
        )
        assert r.status_code == 200 and r.json()["accepted"] == 1
    rows = {
        r["user_id"]: r["entity"]
        for r in await db_admin.fetch("select user_id, entity from events where event_id = $1", UUID(shared))
    }
    assert rows == {a: {"ticker": "NVDA"}, b: {"ticker": "AAPL"}}


async def test_per_user_rate_limit_trips_and_recovers(
    app_factory: Callable[..., Any], make_user: Any, auth_headers: Any
) -> None:
    a, b = await make_user(), await make_user()
    clock = FakeClock(datetime.now(UTC))
    async with app_factory(clock=clock, rate_limit_user_burst=10_000, rate_limit_ip_burst=10_000) as c:
        ha = auth_headers(a)
        for _ in range(3):  # 3 x 100 = burst of 300
            r = await c.post(
                "/v1/events",
                json={"events": [good(ts=clock.now().isoformat()) for _ in range(100)]},
                headers=ha,
            )
            assert r.status_code == 200
        r = await c.post(
            "/v1/events", json={"events": [good(ts=clock.now().isoformat()) for _ in range(20)]}, headers=ha
        )
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1 and r.json()["details"]["retry_after_s"] >= 1
        other = await c.post(
            "/v1/events", json={"events": [good(ts=clock.now().isoformat())]}, headers=auth_headers(b)
        )
        assert other.status_code == 200  # other users unaffected
        clock.advance(2)  # 20/s refill
        r = await c.post(
            "/v1/events", json={"events": [good(ts=clock.now().isoformat()) for _ in range(20)]}, headers=ha
        )
        assert r.status_code == 200


async def test_rls_user_cannot_read_others_events(make_user: Any, db_admin: Any, db_as_user: Any) -> None:
    a, b = await make_user(), await make_user()
    await db_admin.execute(
        "insert into events (event_id, user_id, ts, type) values ($1, $2, now(), 'ticker_viewed')", uuid4(), a
    )
    async with db_as_user(b) as conn:
        assert await conn.fetchval("select count(*) from events") == 0
