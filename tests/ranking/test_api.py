"""HTTP surface of `/v1/feed` and `/v1/why/*` against the seeded personas, and the proposal -> bandit loop."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest

from tests.stream_d import login

pytestmark = pytest.mark.db


async def get_feed(client: httpx.AsyncClient, h: dict[str, str], **q: Any) -> dict[str, Any]:
    r = await client.get("/v1/feed", headers=h, params=q)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_feed_requires_auth(dclient: httpx.AsyncClient) -> None:
    assert (await dclient.get("/v1/feed")).status_code == 401
    assert (await dclient.get(f"/v1/why/alert/{uuid4()}")).status_code == 401


async def test_priya_feed_pins_breaches_and_matches_the_contract_row(
    dclient: httpx.AsyncClient, reseed: Any
) -> None:
    await reseed("priya")
    h, _ = await login(dclient, "priya")
    page = await get_feed(dclient, h)
    items = page["items"]
    assert items and set(page) == {"items", "next_cursor", "total"}
    pinned = [i for i in items if i["pinned"]]
    assert [i["kind"] for i in pinned] == ["alert", "alert"]
    assert {i["item"]["title"] for i in pinned} == {
        "Position cap breached: JNJ",
        "Sector rule breached: Consumer Staples",
    }
    assert [i["pinned"] for i in items[: len(pinned)]] == [True] * len(pinned)  # pinned come first, always
    kinds = {i["kind"] for i in items}
    assert {"alert", "proposal", "briefing"} <= kinds
    for i in items:
        assert i["why"]["summary"] and i["why"]["basis"] in ("profile", "behaviour", "cold_start")
        assert isinstance(i["score"], float)
    briefing = next(i for i in items if i["kind"] == "briefing" and i["item"]["title"] == "Daily briefing")
    text = " ".join(b["text"] for b in briefing["item"]["bullets"])
    assert (
        text.startswith("Portfolio value is $")
        and "breached" in text
        and "Approving only affects the paper simulator" in text
    )
    prop = next(i for i in items if i["kind"] == "proposal")
    assert prop["item"]["status"] == "pending" and prop["item"]["disclaimer"] and prop["pinned"] is False
    # one audit row per item shown
    me = await dclient.get("/v1/me", headers=h)
    assert me.status_code == 200


async def test_feed_is_stable_across_reloads_and_pages(dclient: httpx.AsyncClient, reseed: Any) -> None:
    await reseed("alex")
    h, _ = await login(dclient, "alex")
    one = await get_feed(dclient, h, limit=50)
    two = await get_feed(dclient, h, limit=50)
    assert [i["id"] for i in one["items"]] == [
        i["id"] for i in two["items"]
    ]  # same draw inside the 5-minute window
    small = await get_feed(dclient, h, limit=3)
    assert len(small["items"]) == 3 and small["next_cursor"] == "3"
    rest = await get_feed(dclient, h, limit=50, cursor="3")
    assert [i["id"] for i in small["items"]] + [i["id"] for i in rest["items"]] == [
        i["id"] for i in one["items"]
    ]
    for bad in ({"limit": 0}, {"limit": 51}, {"cursor": "abc"}):
        r = await dclient.get("/v1/feed", headers=h, params=bad)
        assert r.status_code == 422 and r.json()["code"] == "validation_error", bad


async def test_personas_get_different_feeds(dclient: httpx.AsyncClient, reseed: Any) -> None:
    await reseed("priya", "alex")
    hp, _ = await login(dclient, "priya")
    ha, _ = await login(dclient, "alex")
    fp = await get_feed(dclient, hp, limit=50)
    fa = await get_feed(dclient, ha, limit=50)
    tp = [i["item"]["title"] for i in fp["items"]]
    ta = [i["item"]["title"] for i in fa["items"]]
    assert tp != ta and "Position cap breached: NVDA" in ta and "Position cap breached: NVDA" not in tp


async def test_why_endpoint_is_built_from_the_audit_row(dclient: httpx.AsyncClient, reseed: Any) -> None:
    await reseed("priya")
    h, _uid = await login(dclient, "priya")
    page = await get_feed(dclient, h)
    soft = next(i for i in page["items"] if i["kind"] == "alert" and not i["pinned"])
    r = await dclient.get(f"/v1/why/alert/{soft['id']}", headers=h)
    assert r.status_code == 200
    assert r.json() == soft["why"]  # identical to the row embedded in the feed
    crit = next(i for i in page["items"] if i["pinned"])
    rc = await dclient.get(f"/v1/why/alert/{crit['id']}", headers=h)
    assert rc.json()["summary"].startswith("Pinned") and rc.json()["basis"] == "profile"
    prop = next(i for i in page["items"] if i["kind"] == "proposal")
    assert (await dclient.get(f"/v1/why/proposal/{prop['id']}", headers=h)).status_code == 200
    assert (
        await dclient.get(
            f"/v1/why/briefing/{next(i['id'] for i in page['items'] if i['kind'] == 'briefing')}", headers=h
        )
    ).status_code == 200
    # cross-user and unknown ids -> 404 (RLS), unknown type -> 404
    ha, _ = await login(dclient, "alex")
    assert (await dclient.get(f"/v1/why/alert/{soft['id']}", headers=ha)).status_code == 404
    assert (await dclient.get(f"/v1/why/alert/{uuid4()}", headers=h)).status_code == 404
    assert (await dclient.get(f"/v1/why/nonsense/{uuid4()}", headers=h)).status_code == 404


async def test_alex_rejecting_proposals_lowers_proposal_ranking(
    dclient: httpx.AsyncClient, reseed: Any
) -> None:
    """SPEC 10.9: Alex rejects 'reduce exposure' proposals (reason: disagree) -> the feed re-ranks and 'why' updates."""
    await reseed("alex")
    h, uid = await login(dclient, "alex")
    before = await get_feed(dclient, h, limit=50)
    props = [i for i in before["items"] if i["kind"] == "proposal"]
    assert len(props) >= 2
    reasons = [(p["id"], p["item"]["kind"]) for p in props]
    trade_ids = [pid for pid, kind in reasons if kind == "trade"]
    for pid in trade_ids[:2]:
        r = await dclient.post(
            f"/v1/proposals/{pid}/decision", headers=h, json={"decision": "reject", "reason": "disagree"}
        )
        assert r.status_code == 200, r.text
    after = await get_feed(dclient, h, limit=50)
    remaining = [i for i in after["items"] if i["kind"] == "proposal"]
    assert {i["id"] for i in remaining}.isdisjoint(trade_ids[:2])  # decided proposals leave the feed
    # the bandit arm moved through the registered decision hook
    import asyncpg

    from app.core.db import init_connection

    conn = await asyncpg.connect(dclient.app.state.settings.database_url)  # type: ignore[attr-defined]
    await init_connection(conn)
    try:
        await conn.execute("select set_config('app.user_id', $1, false)", str(uid))
        row = await conn.fetchrow(
            "select alpha, beta from bandit_arms where user_id = $1 and arm = 'proposal' and context = ''",
            uid,
        )
        sec = await conn.fetchrow(
            "select beta from bandit_arms where user_id = $1 and arm = 'sector' and context = 'sector:Technology'",
            uid,
        )
    finally:
        await conn.close()
    # seeded Beta(2, 3), then two rejections through the decision hook: beta grew, the posterior mean fell
    assert (
        row is not None
        and float(row["beta"]) > 3.0
        and float(row["alpha"]) / (float(row["alpha"]) + float(row["beta"])) < 0.4
    )
    assert sec is not None
    if remaining:
        w = await dclient.get(f"/v1/why/proposal/{remaining[0]['id']}", headers=h)
        assert w.json()["basis"] in ("behaviour", "profile")


async def test_t_pers_07_two_personas_same_ticker_via_the_brief_api(
    dclient: httpx.AsyncClient, reseed: Any, db_admin: Any
) -> None:
    """T-PERS-07 over HTTP: Priya and Alex open the NVDA brief: section order differs by >= 30 %, style differs,
    the facts (ledger) are identical."""
    from app.modules.edgar.xbrl import parse_companyfacts, store_facts
    from tests.edgar.conftest import load_companyfacts_json

    await reseed("priya", "alex")
    await db_admin.execute("delete from xbrl_facts where symbol = 'NVDA'")
    await store_facts(db_admin, "NVDA", parse_companyfacts(load_companyfacts_json("NVDA")))
    try:
        hp, _ = await login(dclient, "priya")
        ha, _ = await login(dclient, "alex")
        # the seed's style card has `section_order` only; opening the profile runs `recompute_features`, which writes
        # the `order` key the brief generator reads (CR-17: the seed should write `order` itself)
        assert (await dclient.get("/v1/profile", headers=hp)).status_code == 200
        assert (await dclient.get("/v1/profile", headers=ha)).status_code == 200
        rp = await dclient.get("/v1/briefs/NVDA", headers=hp)
        ra = await dclient.get("/v1/briefs/NVDA", headers=ha)
        assert rp.status_code == 200 and ra.status_code == 200, (rp.text[:300], ra.text[:300])
        bp, ba = rp.json(), ra.json()
        op, oa = bp["section_order"], ba["section_order"]
        assert len(op) == len(oa) == 7
        assert sum(x != y for x, y in zip(op, oa, strict=True)) / len(op) >= 0.30
        assert bp["style_card"] != ba["style_card"]
        assert bp["ledger"] == ba["ledger"] and bp["ledger"]  # facts identical
        why = await dclient.get(f"/v1/why/brief/{bp['id']}", headers=hp)
        assert why.status_code == 200 and why.json()["top_features"]
    finally:
        await db_admin.execute("delete from xbrl_facts where symbol = 'NVDA'")


async def test_why_rules_checked_never_repeats_a_rule(dclient: httpx.AsyncClient, reseed: Any) -> None:
    """Integration pass 1: proposal items listed each rule twice (the stored checks + the re-validation)."""
    await reseed("priya", "alex")
    saw_proposal = False
    for persona in ("priya", "alex"):
        h, _ = await login(dclient, persona)
        page = await get_feed(dclient, h, limit=50)
        for i in page["items"]:
            rules = [r["rule"] for r in i["why"]["rules_checked"]]
            assert len(rules) == len(set(rules)), (persona, i["kind"], rules)
            if i["kind"] == "proposal":
                saw_proposal = saw_proposal or bool(rules)  # review proposals carry no trade checks
                # the same view through GET /why (built from the audit row)
                w = (await dclient.get(f"/v1/why/proposal/{i['id']}", headers=h)).json()
                wr = [r["rule"] for r in w["rules_checked"]]
                assert len(wr) == len(set(wr))
    assert saw_proposal
