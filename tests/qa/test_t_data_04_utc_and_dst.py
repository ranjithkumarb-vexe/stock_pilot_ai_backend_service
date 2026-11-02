"""QA-01 / T-DATA-04: "Timezone / DST. Stored UTC; displayed in user TZ; DST test passes".

Backend half of the scenario (the display half is a browser concern, see docs/qa/SCENARIO_MATRIX.md):

* every date-time column in the schema is `timestamptz` (an instant, never a naive local time);
* the API only ever emits explicit-UTC timestamps (`...Z`), so the client can render them in the user's zone;
* the exchange session boundaries move in UTC across the US DST change (EST -> EDT on Sunday 2026-03-08, back on
  2026-11-01) while the New York wall-clock times stay 09:30/16:00.
tests/marketdata/test_calendar.py covers the calendar functions; this file proves it through the HTTP API.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.core.clock import FakeClock

ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


async def test_no_naive_timestamp_columns_exist(db_admin: Any) -> None:
    rows = await db_admin.fetch(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_schema = 'public' and data_type in ('timestamp without time zone', 'time without time zone', "
        "'time with time zone')"
    )
    assert [tuple(r) for r in rows] == [], "date-times must be timestamptz (stored as UTC instants)"
    n = await db_admin.fetchval(
        "select count(*) from information_schema.columns where table_schema = 'public' and data_type = 'timestamp with time zone'"
    )
    assert n > 30, "sanity: the schema really has timestamptz columns"


async def test_a_timestamptz_value_is_the_same_instant_whatever_the_session_zone(db_admin: Any) -> None:
    await db_admin.execute("set time zone 'Asia/Kolkata'")
    try:
        got = await db_admin.fetchval("select '2026-03-09 09:30:00-04'::timestamptz")
    finally:
        await db_admin.execute("reset time zone")
    assert got == datetime(2026, 3, 9, 13, 30, tzinfo=UTC) and got.utcoffset().total_seconds() == 0


def _walk(node: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(node, dict):
        return [x for k, v in node.items() for x in _walk(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [x for i, v in enumerate(node) for x in _walk(v, f"{path}[{i}]")]
    return [(path, node)] if isinstance(node, str) and ISO_DT.match(node) else []


async def _status(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    r = await client.get("/v1/market/status", headers=headers)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


@pytest.mark.parametrize(
    ("now_utc", "next_open", "next_close", "label"),
    [
        # Friday before the change: EST, UTC-5 -> 09:30 ET = 14:30Z, 16:00 ET = 21:00Z
        (
            datetime(2026, 3, 6, 15, 0, tzinfo=UTC),
            "2026-03-09T13:30:00Z",
            "2026-03-06T21:00:00Z",
            "before spring forward",
        ),
        # Monday after: EDT, UTC-4 -> 13:30Z / 20:00Z
        (
            datetime(2026, 3, 9, 15, 0, tzinfo=UTC),
            "2026-03-10T13:30:00Z",
            "2026-03-09T20:00:00Z",
            "after spring forward",
        ),
        # Friday 2026-10-30 is still EDT; Monday 2026-11-02 is EST again (fall back on 2026-11-01)
        (
            datetime(2026, 10, 30, 15, 0, tzinfo=UTC),
            "2026-11-02T14:30:00Z",
            "2026-10-30T20:00:00Z",
            "before fall back",
        ),
        (
            datetime(2026, 11, 2, 15, 0, tzinfo=UTC),
            "2026-11-03T14:30:00Z",
            "2026-11-02T21:00:00Z",
            "after fall back",
        ),
    ],
)
async def test_session_boundaries_follow_new_york_time_across_dst_and_are_emitted_in_utc(
    app_factory: Callable[..., Any],
    make_user: Any,
    auth_headers: Callable[..., dict[str, str]],
    now_utc: datetime,
    next_open: str,
    next_close: str,
    label: str,
) -> None:
    uid = await make_user()
    async with app_factory(clock=FakeClock(now_utc)) as client:
        s = await _status(client, auth_headers(uid))
    assert s["open"] is True, label
    assert s["next_close"] == next_close, label
    assert s["next_open"] == next_open, label
    stamps = _walk(s)
    assert stamps, "the status payload carries timestamps"
    assert all(v.endswith("Z") for _, v in stamps), f"non-UTC timestamps in {stamps}"
