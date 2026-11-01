"""HTTP surface of the alerts API against the seeded personas (Priya / Alex)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from tests.stream_d import login

pytestmark = pytest.mark.db


async def alerts(client: httpx.AsyncClient, h: dict[str, str], **q: Any) -> dict[str, Any]:
    r = await client.get("/v1/alerts", headers=h, params=q)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_auth_required(dclient: httpx.AsyncClient) -> None:
    assert (await dclient.get("/v1/alerts")).status_code == 401
    assert (await dclient.post("/v1/alerts/refresh")).status_code == 401
    assert (await dclient.post(f"/v1/alerts/{uuid4()}/action", json={"action": "open"})).status_code == 401


async def test_priya_has_the_design_alert_set_and_refresh_adds_no_duplicates(
    dclient: httpx.AsyncClient, reseed: Any
) -> None:
    """Seeded Priya keeps the design's JNJ / Consumer Staples breaches (now owned by the real risk engine)."""
    await reseed("priya")
    h, _uid = await login(dclient, "priya")
    before = await alerts(dclient, h)
    titles = {a["title"] for a in before["items"]}
    assert {"Position cap breached: JNJ", "Sector rule breached: Consumer Staples"} <= titles
    assert len(before["items"]) == 7 and before["move_threshold"] == 2.0 and before["fatigue_index"] == 0.0
    crit = [a for a in before["items"] if a["critical"]]
    assert {a["type"] for a in crit} == {"breach"} and all(a["state"] == "new" for a in crit)
    r = await dclient.post("/v1/alerts/refresh", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    after = await alerts(dclient, h)
    breaches = [a for a in after["items"] if a["type"] == "breach"]
    assert len(breaches) == 2, "the engine adopted the seeded breach rows instead of duplicating them"
    assert body["created"] == len(after["items"]) - 7 and body["move_threshold"] == 2.0
    r2 = await dclient.post("/v1/alerts/refresh", headers=h)
    assert r2.json()["created"] == 0 and r2.json()["items"] == []


async def test_alex_refresh_keeps_nvda_and_technology_breaches(
    dclient: httpx.AsyncClient, reseed: Any
) -> None:
    await reseed("alex")
    h, _ = await login(dclient, "alex")
    await dclient.post("/v1/alerts/refresh", headers=h)
    data = await alerts(dclient, h, type="breach")
    assert {a["title"] for a in data["items"]} >= {
        "Position cap breached: NVDA",
        "Sector rule breached: Technology",
    }
    assert all(a["type"] == "breach" for a in data["items"])


async def test_filters_state_aliases_and_pagination(dclient: httpx.AsyncClient, reseed: Any) -> None:
    await reseed("priya")
    h, _ = await login(dclient, "priya")
    filings = await alerts(dclient, h, type="filing")
    assert {a["type"] for a in filings["items"]} == {"filing"} and len(filings["items"]) == 2
    page1 = await alerts(dclient, h, limit=3)
    assert len(page1["items"]) == 3 and page1["next_cursor"]
    page2 = await alerts(dclient, h, limit=3, cursor=page1["next_cursor"])
    page3 = await alerts(dclient, h, limit=3, cursor=page2["next_cursor"])
    ids = [a["id"] for p in (page1, page2, page3) for a in p["items"]]
    assert len(ids) == len(set(ids)) == 7 and page3["next_cursor"] is None
    created = [a["created_at"] for p in (page1, page2, page3) for a in p["items"]]
    assert created == sorted(created, reverse=True)
    assert (await alerts(dclient, h, state="new"))["items"]
    assert (await alerts(dclient, h, state="read"))["items"] == []  # alias of `opened`
    assert (await alerts(dclient, h, state="archived"))["items"] == []
    for bad in ({"state": "bogus"}, {"type": "bogus"}, {"cursor": "!!"}, {"limit": 0}, {"limit": 999}):
        r = await dclient.get("/v1/alerts", headers=h, params=bad)
        assert r.status_code == 422 and r.json()["code"] == "validation_error", bad


async def test_actions_over_http_with_contract_errors(dclient: httpx.AsyncClient, reseed: Any) -> None:
    await reseed("priya")
    h, _ = await login(dclient, "priya")
    items = (await alerts(dclient, h))["items"]
    crit = next(a for a in items if a["critical"])
    soft = [a for a in items if not a["critical"]]
    r = await dclient.post(f"/v1/alerts/{soft[0]['id']}/action", headers=h, json={"action": "open"})
    assert r.status_code == 200 and r.json()["state"] == "opened"
    r = await dclient.post(f"/v1/alerts/{soft[1]['id']}/action", headers=h, json={"action": "dismiss"})
    assert r.status_code == 200 and r.json()["state"] == "dismissed"
    r = await dclient.post(f"/v1/alerts/{soft[2]['id']}/action", headers=h, json={"action": "snooze"})
    assert r.json()["state"] == "snoozed" and r.json()["snoozed_until"]
    r = await dclient.post(f"/v1/alerts/{soft[1]['id']}/action", headers=h, json={"action": "restore"})
    assert r.json()["state"] == "new"
    for action in ("dismiss", "snooze"):  # criticals are pinned
        r = await dclient.post(f"/v1/alerts/{crit['id']}/action", headers=h, json={"action": action})
        assert r.status_code == 409 and r.json()["code"] == "conflict" and "pinned" in r.json()["message"]
    r = await dclient.post(f"/v1/alerts/{crit['id']}/action", headers=h, json={"action": "open"})
    assert r.status_code == 200 and r.json()["critical"] is True
    r = await dclient.post(f"/v1/alerts/{uuid4()}/action", headers=h, json={"action": "open"})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    for body in ({"action": "explode"}, {}, {"action": "open", "extra": 1}):
        assert (
            await dclient.post(f"/v1/alerts/{soft[0]['id']}/action", headers=h, json=body)
        ).status_code == 422
    arch = await alerts(dclient, h, state="archived")
    assert {a["state"] for a in arch["items"]} == {"snoozed"}
    # 24 h later the snooze has expired and the alert is back
    dclient.clock.advance(hours=25)  # type: ignore[attr-defined]
    back = await alerts(dclient, h, state="new")
    assert soft[2]["id"] in {a["id"] for a in back["items"]}


async def test_other_users_cannot_see_or_act_on_alerts(dclient: httpx.AsyncClient, reseed: Any) -> None:
    await reseed("priya", "alex")
    hp, _ = await login(dclient, "priya")
    ha, _ = await login(dclient, "alex")
    pid = (await alerts(dclient, hp))["items"][0]["id"]
    assert pid not in {a["id"] for a in (await alerts(dclient, ha))["items"]}
    r = await dclient.post(f"/v1/alerts/{pid}/action", headers=ha, json={"action": "open"})
    assert r.status_code == 404


async def test_dismissals_over_http_raise_the_move_threshold_shown_on_the_list(
    dclient: httpx.AsyncClient, reseed: Any
) -> None:
    await reseed("alex")
    h, _uid = await login(dclient, "alex")
    app = dclient.app  # type: ignore[attr-defined]
    soft = [a for a in (await alerts(dclient, h))["items"] if not a["critical"]]
    for a in soft[:5]:
        await dclient.post(f"/v1/alerts/{a['id']}/action", headers=h, json={"action": "dismiss"})
    data = await alerts(dclient, h)
    assert data["move_threshold"] > 2.0 and data["fatigue_index"] == 1.0 and data["min_materiality"] > 0.3
    assert app.state.alert_monitor is not None


async def test_refresh_publishes_new_alerts_over_sse_after_commit(
    dclient: httpx.AsyncClient, reseed: Any
) -> None:
    await reseed("priya")
    h, uid = await login(dclient, "priya")
    hub = dclient.app.state.stream_hub  # type: ignore[attr-defined]
    sub = hub.subscribe(UUID(str(uid)), [])
    # a brand-new 8-K for a holding arrives from a source
    from datetime import datetime

    from app.modules.alerts.detectors import FilingSignal

    class Src:
        name = "sec_edgar"

        async def poll(self, symbols: Any, since: datetime) -> list[FilingSignal]:
            now = dclient.clock.now()  # type: ignore[attr-defined]
            return [
                FilingSignal(
                    "PG",
                    "0000080424-26-000777",
                    ("5.02", "9.01"),
                    now - timedelta(minutes=2),
                    "sec_edgar",
                    None,
                )
            ]

    dclient.app.state.alert_monitor.sources = [Src()]  # type: ignore[attr-defined]
    r = await dclient.post("/v1/alerts/refresh", headers=h)
    assert r.status_code == 200
    got = [e for e in sub.drain() if e.type == "alert"]
    assert [e.data["title"] for e in got] == [i["title"] for i in r.json()["items"]]
    assert any(i["symbol"] == "PG" and i["type"] == "filing" for i in r.json()["items"])
    hub.unsubscribe(sub)


async def test_seed_hook_adopts_the_design_breaches_and_is_idempotent(
    dclient: httpx.AsyncClient, reseed: Any, seed_now: Any, db_admin: Any
) -> None:
    from app.core.personas import PERSONAS
    from app.modules.alerts.seedhook import refresh_for_seed

    await reseed("priya")
    uid = PERSONAS["priya"].user_id
    before = await db_admin.fetchval(
        "select count(*) from alerts where user_id = $1 and type = 'breach'", uid
    )
    await refresh_for_seed(db_admin, uid, seed_now)
    await refresh_for_seed(db_admin, uid, seed_now)
    rows = await db_admin.fetch("select payload from alerts where user_id = $1 and type = 'breach'", uid)
    assert before == len(rows) == 2 and all(r["payload"].get("breach_key") for r in rows)
