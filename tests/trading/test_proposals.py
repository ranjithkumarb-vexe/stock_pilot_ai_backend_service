"""BE-10: proposal workflow (FR-05) - generation from the risk engine, decisions, paper execution, wording."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.modules.paper.state import PortfolioState, PositionState
from app.modules.proposals import hooks, service, templates
from app.modules.risk import Rules
from app.modules.tradingcommon.disclaimer import DISCLAIMER_TEXT
from tests.trading.support import StaleSource, login, place

pytestmark = pytest.mark.db
GOLDEN = json.loads((Path(__file__).parent / "golden" / "design_parity.json").read_text())
D = Decimal


async def proposals(
    client: httpx.AsyncClient, h: dict[str, str], status: str | None = None
) -> list[dict[str, Any]]:
    r = await client.get("/v1/proposals" + (f"?status={status}" if status else ""), headers=h)
    assert r.status_code == 200, r.text
    items: list[dict[str, Any]] = r.json()["items"]
    return items


def by_rule(items: list[dict[str, Any]], rule: str) -> dict[str, Any]:
    return next(p for p in items if p["rule"] == rule)


async def decide(client: httpx.AsyncClient, h: dict[str, str], pid: str, **body: Any) -> httpx.Response:
    return await client.post(f"/v1/proposals/{pid}/decision", headers=h, json=body)


# ------------------------------------------------------------------------------------------ generation


@pytest.mark.parametrize("persona", ["priya", "alex"])
async def test_seeded_proposals_match_the_design_pq(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], persona: str
) -> None:
    await reseed(persona)
    h, _ = await login(tclient, persona)
    items = await proposals(tclient, h, "pending")
    g = {(p["tk"], p["rule"]): p for p in GOLDEN["personas"][persona]["proposals"]}
    trades = [p for p in items if p["kind"] == "trade"]
    assert {(p["symbol"], p["rule"]) for p in trades} == set(g)
    for p in trades:
        want = g[(p["symbol"], p["rule"])]
        assert p["side"] == "sell" and p["suggested_qty"] == want["q"]  # EXACTLY the design's pq
        assert p["current_weight_pct"] == pytest.approx(want["w"], abs=0.01)
        assert p["after_weight_pct"] == pytest.approx(want["aw"], abs=0.01)
        assert p["cap_pct"] == want["cap"]
    reviews = {p["rule"] for p in items if p["kind"] == "review"}
    assert reviews == ({"drop"} if persona == "priya" else {"overtrading"})
    assert len(items) == 3  # the design shows exactly three pending proposals per persona


async def test_proposal_shape_matches_the_contract(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya")
    h, _ = await login(tclient, "priya")
    p = by_rule(await proposals(tclient, h), "pos")
    assert {
        "id", "kind", "symbol", "side", "rule", "title", "body", "rationale", "rules_checked", "suggested_qty", "current_weight_pct",
        "after_weight_pct", "cap_pct", "sources", "status", "expires_at", "created_at", "disclaimer", "why",
    } <= set(p)  # fmt: skip
    assert p["title"] == "Position above your 10% cap" and p["symbol"] == "JNJ" and p["kind"] == "trade"
    assert p["body"].startswith(
        "Your rule max position 10% is exceeded: JNJ is 10.7% of your portfolio. Options: reduce by at least 0.7 pp"
    )
    assert p["body"].endswith("Selling 4 shares brings it to 10.0%.")
    assert (
        p["rationale"][0]
        == "Your rule: maximum position 10%. JNJ is 10.7% of your portfolio. Selling 4 shares brings it to 10.0%."
    )
    assert p["sources"] == ["Risk engine · rule R-POS"] and p["disclaimer"] == DISCLAIMER_TEXT
    assert (
        all(c["passed"] for c in p["rules_checked"]) and p["rules_checked"][0]["rule"] == "No shorting (MVP)"
    )
    assert p["why"]["basis"] == "profile" and p["why"]["top_features"][0]["name"] == "rule_breach"
    assert p["status"] == "pending" and p["expires_at"] > p["created_at"]
    sec = by_rule(await proposals(tclient, h), "sec")
    assert sec["title"] == "Consumer Staples above your 25% sector rule" and sec["symbol"] == "PEP"
    drop = by_rule(await proposals(tclient, h), "drop")
    assert (
        drop["kind"] == "review"
        and drop["symbol"] == "VZ"
        and drop["suggested_qty"] == 0
        and drop["side"] is None
    )
    assert drop["title"] == "Review Verizon Communications after the 4.1% price drop"
    assert "You hold 215 shares" in drop["body"] and "Consider reviewing the key risks" in drop["body"]
    one = (await tclient.get(f"/v1/proposals/{p['id']}", headers=h)).json()
    assert one["id"] == p["id"]
    assert (await tclient.get(f"/v1/proposals/{uuid4()}", headers=h)).status_code == 404
    assert (await tclient.get("/v1/proposals?status=bogus", headers=h)).status_code == 422


async def test_alex_overtrading_review_uses_the_study_text(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("alex")
    h, _ = await login(tclient, "alex")
    p = by_rule(await proposals(tclient, h), "overtrading")
    assert p["title"] == "Review your trading pace: 9 trades in 7 days" and p["symbol"] is None
    assert p["body"] == (
        "Your rule allows 10 trades per week. In a study of 66,465 households, those who traded most earned 11.4% a year "
        "against 17.9% for the market. Consider reviewing whether each trade matched a written reason."
    )


async def test_refresh_is_idempotent_and_dedupes_per_symbol_and_rule(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(tclient, "priya")
    first = (await tclient.post("/v1/proposals/refresh", headers=h)).json()
    assert first["created"] == 0 and first["open"] == 3  # already generated by the seed
    again = (await tclient.post("/v1/proposals/refresh", headers=h)).json()
    assert again == {"created": 0, "expired": 0, "open": 3, "items": []}
    # the database enforces it too: a second pending (symbol, rule) cannot be inserted
    row = await db_admin.fetchrow(
        "select portfolio_id, symbol, payload from proposals where user_id = $1 and symbol = 'JNJ'", uid
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db_admin.execute(
            "insert into proposals (user_id, portfolio_id, type, symbol, payload, status) values ($1, $2, 'trade', 'JNJ', $3, 'pending')",
            uid, row["portfolio_id"], row["payload"],
        )  # fmt: skip
    assert (await tclient.post("/v1/proposals/refresh")).status_code == 401


async def test_sam_has_no_proposals(tclient: httpx.AsyncClient, reseed: Callable[..., Any]) -> None:
    await reseed("sam")
    h, _ = await login(tclient, "sam")
    assert await proposals(tclient, h) == []
    assert (await tclient.post("/v1/proposals/refresh", headers=h)).json()["open"] == 0


# ------------------------------------------------------------------------------------------ approve


async def test_approve_executes_exactly_one_paper_order_referencing_the_proposal(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(tclient, "priya")
    p = by_rule(await proposals(tclient, h), "pos")
    r = await decide(tclient, h, p["id"], decision="approve")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["proposal"]["status"] == "approved" and j["proposal"]["order_id"] == j["order"]["id"]
    assert j["order"]["proposal_id"] == p["id"] and j["order"]["source"] == "proposal"
    assert j["order"]["side"] == "sell" and j["order"]["symbol"] == "JNJ" and j["order"]["filled_qty"] == 4
    assert j["order"]["status"] == "filled"
    assert (
        await db_admin.fetchval(
            "select count(*) from orders where proposal_id = $1",
            p["id"] if False else __import__("uuid").UUID(p["id"]),
        )
        == 1
    )
    held = await db_admin.fetchval("select qty from positions where user_id = $1 and symbol = 'JNJ'", uid)
    assert held == 58
    # decided proposals cannot be decided again; no second order appears
    again = await decide(tclient, h, p["id"], decision="approve")
    assert again.status_code == 409 and again.json()["details"]["status"] == "approved"
    assert (
        await db_admin.fetchval(
            "select count(*) from orders where proposal_id = $1", __import__("uuid").UUID(p["id"])
        )
        == 1
    )
    # events + audit trail
    ev = await db_admin.fetch(
        "select type, entity, props from events where user_id = $1 and type = 'proposal_approved'", uid
    )
    assert (
        len(ev) == 1 and ev[0]["entity"]["proposal_id"] == p["id"] and ev[0]["props"]["decision"] == "approve"
    )
    acts = {
        r_["action"] for r_ in await db_admin.fetch("select action from audit_log where user_id = $1", uid)
    }
    assert {"proposal.create", "proposal.approve", "order.create"} <= acts
    # the breach is resolved and is NOT regenerated (decided proposals are not re-created for 24 h)
    items = await proposals(tclient, h, "pending")
    assert not any(i["rule"] == "pos" for i in items)


async def test_approve_review_places_no_order(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(tclient, "priya")
    p = by_rule(await proposals(tclient, h), "drop")
    n = await db_admin.fetchval("select count(*) from orders where user_id = $1", uid)
    r = await decide(tclient, h, p["id"], decision="approve")
    assert r.status_code == 200 and r.json()["order"] is None and r.json()["proposal"]["status"] == "approved"
    assert await db_admin.fetchval("select count(*) from orders where user_id = $1", uid) == n
    assert (
        await decide(
            tclient, h, by_rule(await proposals(tclient, h, "pending"), "pos")["id"], decision="modify", qty=1
        )
    ).status_code == 200
    rev = by_rule(await proposals(tclient, h, "pending"), "sec")
    assert (await decide(tclient, h, rev["id"], decision="approve")).status_code == 200


async def test_market_closed_approval_queues_the_order(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya")
    h, _ = await login(tclient, "priya")
    tclient.app.state.market_override = False  # type: ignore[attr-defined]
    p = by_rule(await proposals(tclient, h), "pos")
    j = (await decide(tclient, h, p["id"], decision="approve")).json()
    assert (
        j["proposal"]["status"] == "approved"
        and j["order"]["status"] == "queued"
        and j["order"]["proposal_id"] == p["id"]
    )


async def test_failed_execution_rolls_the_decision_back(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, _uid = await login(tclient, "priya")
    p = by_rule(await proposals(tclient, h), "pos")
    good = tclient.app.state.price_source  # type: ignore[attr-defined]
    tclient.app.state.price_source = StaleSource(good)  # type: ignore[attr-defined]
    r = await decide(tclient, h, p["id"], decision="approve")
    assert r.status_code == 409 and r.json()["code"] == "stale_quote"
    assert (await tclient.get(f"/v1/proposals/{p['id']}", headers=h)).json()["status"] == "pending"
    assert (
        await db_admin.fetchval(
            "select count(*) from orders where proposal_id = $1", __import__("uuid").UUID(p["id"])
        )
        == 0
    )
    tclient.app.state.price_source = good  # type: ignore[attr-defined]
    assert (await decide(tclient, h, p["id"], decision="approve")).status_code == 200


# ------------------------------------------------------------------------------------------ reject / modify


async def test_reject_stores_the_reason_chip_and_blocks_regeneration_for_24h(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("alex")
    h, uid = await login(tclient, "alex")
    p = by_rule(await proposals(tclient, h), "pos")
    assert p["symbol"] == "NVDA"
    assert (await decide(tclient, h, p["id"], decision="reject", reason="lol")).status_code == 422
    r = await decide(tclient, h, p["id"], decision="reject", reason="disagree")
    assert r.status_code == 200 and r.json()["order"] is None
    assert (
        r.json()["proposal"]["status"] == "rejected" and r.json()["proposal"]["reject_reason"] == "disagree"
    )
    assert (
        await db_admin.fetchval(
            "select reject_reason from proposals where id = $1", __import__("uuid").UUID(p["id"])
        )
        == "disagree"
    )
    assert (
        await db_admin.fetchval(
            "select count(*) from orders where proposal_id = $1", __import__("uuid").UUID(p["id"])
        )
        == 0
    )
    ev = await db_admin.fetchrow(
        "select props from events where user_id = $1 and type = 'proposal_rejected' and entity ->> 'proposal_id' = $2",
        uid,
        p["id"],
    )
    assert ev["props"]["reason"] == "disagree"
    # refresh while the breach persists does not nag within 24 h ...
    assert (await tclient.post("/v1/proposals/refresh", headers=h)).json()["created"] == 0
    assert not any(i["rule"] == "pos" for i in await proposals(tclient, h, "pending"))
    # ... and regenerates once the cooldown is over (the user's rule is still broken)
    tclient.clock.advance(hours=25)  # type: ignore[attr-defined]
    again = (await tclient.post("/v1/proposals/refresh", headers=h)).json()
    assert again["created"] >= 1 and any(i["symbol"] == "NVDA" and i["rule"] == "pos" for i in again["items"])


async def test_reject_without_reason_is_allowed(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya")
    h, _ = await login(tclient, "priya")
    p = by_rule(await proposals(tclient, h), "drop")
    r = await decide(tclient, h, p["id"], decision="reject")
    assert r.status_code == 200 and r.json()["proposal"]["reject_reason"] is None


async def test_modify_uses_the_given_quantity(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("alex")
    h, uid = await login(tclient, "alex")
    p = by_rule(await proposals(tclient, h), "pos")  # NVDA, suggests 82
    assert p["suggested_qty"] == 82
    for bad in (0, -3, 220, 1000):
        assert (await decide(tclient, h, p["id"], decision="modify", qty=bad)).status_code == 422
    assert (await decide(tclient, h, p["id"], decision="modify")).status_code == 422  # qty missing
    assert (
        await decide(tclient, h, p["id"], decision="approve", qty=5)
    ).status_code == 422  # qty only with modify
    assert (await decide(tclient, h, p["id"], decision="reject", qty=5)).status_code == 422
    assert (await decide(tclient, h, p["id"], decision="approve", reason="timing")).status_code == 422
    r = await decide(tclient, h, p["id"], decision="modify", qty=30)
    assert r.status_code == 200, r.text
    j = r.json()
    assert (
        j["proposal"]["status"] == "modified"
        and j["proposal"]["modified_qty"] == 30
        and j["proposal"]["executed_qty"] == 30
    )
    assert j["order"]["filled_qty"] == 30 and j["order"]["symbol"] == "NVDA"
    assert (
        await db_admin.fetchval("select qty from positions where user_id = $1 and symbol = 'NVDA'", uid)
        == 219 - 30
    )
    ev = await db_admin.fetchrow(
        "select props from events where user_id = $1 and type = 'proposal_modified'", uid
    )
    assert ev["props"]["qty"] == 30 and ev["props"]["delta"] == pytest.approx(30 / 82, abs=1e-3)


async def test_review_proposals_cannot_be_modified(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("alex")
    h, _ = await login(tclient, "alex")
    p = by_rule(await proposals(tclient, h), "overtrading")
    r = await decide(tclient, h, p["id"], decision="modify", qty=1)
    assert r.status_code == 422


# ------------------------------------------------------------------------------------------ expiry / resolution / tenancy


async def test_proposals_expire_after_24h_and_cannot_be_decided(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, _uid = await login(tclient, "priya")
    p = by_rule(await proposals(tclient, h), "pos")
    tclient.clock.advance(hours=24, seconds=1)  # type: ignore[attr-defined]
    r = await decide(tclient, h, p["id"], decision="approve")
    assert (
        r.status_code == 409
        and r.json()["details"]["status"] == "expired"
        and "expired" in r.json()["message"]
    )
    assert (await tclient.get(f"/v1/proposals/{p['id']}", headers=h)).json()["status"] == "expired"
    assert (
        await db_admin.fetchval(
            "select count(*) from orders where proposal_id = $1", __import__("uuid").UUID(p["id"])
        )
        == 0
    )
    assert len(await proposals(tclient, h, "expired")) == 3
    # a refresh creates fresh proposals for the still-broken rules
    fresh = (await tclient.post("/v1/proposals/refresh", headers=h)).json()
    assert fresh["created"] == 3 and fresh["open"] == 3


async def test_a_resolved_breach_withdraws_the_pending_proposal(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, _uid = await login(tclient, "priya")
    pos = by_rule(await proposals(tclient, h), "pos")
    r = await place(tclient, h, "JNJ", "sell", 10)  # the user fixes it themselves
    assert r.status_code == 201
    # the after-trade hook already refreshed
    row = await db_admin.fetchrow(
        "select status, payload from proposals where id = $1", __import__("uuid").UUID(pos["id"])
    )
    assert row["status"] == "expired" and row["payload"]["resolved"] is True
    assert (await decide(tclient, h, pos["id"], decision="approve")).status_code == 409


async def test_pending_proposal_for_a_closed_position_is_withdrawn_on_approve(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(tclient, "priya")
    p = by_rule(await proposals(tclient, h), "pos")
    await db_admin.execute(
        "delete from positions where user_id = $1 and symbol = 'JNJ'", uid
    )  # simulate a stale proposal
    r = await decide(tclient, h, p["id"], decision="approve")
    assert r.status_code == 409 and "no longer applies" in r.json()["message"]


async def test_proposals_are_private_to_their_owner(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya", "alex")
    ph, _ = await login(tclient, "priya")
    ah, _ = await login(tclient, "alex")
    pid = by_rule(await proposals(tclient, ph), "pos")["id"]
    assert (await tclient.get(f"/v1/proposals/{pid}", headers=ah)).status_code == 404
    assert (await decide(tclient, ah, pid, decision="approve")).status_code == 404
    assert pid not in {p["id"] for p in await proposals(tclient, ah)}
    assert (await tclient.get(f"/v1/proposals/{pid}")).status_code == 401


# ------------------------------------------------------------------------------------------ hooks (stream C bandit)


async def test_decision_hook_receives_the_reward(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("alex")
    h, uid = await login(tclient, "alex")
    seen: list[tuple[Any, dict[str, Any], float]] = []

    async def spy(conn: Any, user_id: Any, proposal: dict[str, Any], reward: float) -> None:
        seen.append((user_id, proposal, reward))

    async def broken(conn: Any, user_id: Any, proposal: dict[str, Any], reward: float) -> None:
        await conn.execute("select 1/0")

    hooks.clear_hooks()
    hooks.register_decision_hook(broken)
    hooks.register_decision_hook(spy)
    hooks.register_decision_hook(spy)  # duplicate registration ignored
    try:
        items = await proposals(tclient, h, "pending")
        a, b, c = by_rule(items, "pos"), by_rule(items, "sec"), by_rule(items, "overtrading")
        # (approving the NVDA trim first would also resolve the Technology breach and withdraw `sec`)
        assert (await decide(tclient, h, b["id"], decision="modify", qty=5)).status_code == 200
        assert (await decide(tclient, h, a["id"], decision="approve")).status_code == 200
        assert (await decide(tclient, h, c["id"], decision="reject", reason="timing")).status_code == 200
    finally:
        hooks.clear_hooks()
    assert [(u, p["rule"], r) for u, p, r in seen] == [
        (uid, "sec", 0.5),
        (uid, "pos", 1.0),
        (uid, "overtrading", 0.0),
    ]
    assert seen[1][1]["status"] == "approved" and seen[2][1]["reject_reason"] == "timing"
    # default registry is a no-op
    await hooks.on_decision(None, uid, {}, 1.0)  # type: ignore[arg-type,unused-ignore]


async def test_created_hook_fires_for_new_proposals(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya")
    h, _ = await login(tclient, "priya")
    got: list[str] = []

    async def spy(conn: Any, user_id: Any, proposal: dict[str, Any]) -> None:
        got.append(proposal["rule"])

    hooks.clear_hooks()
    hooks.register_created_hook(spy)
    try:
        tclient.clock.advance(hours=25)  # type: ignore[attr-defined]
        await tclient.post("/v1/proposals/refresh", headers=h)
    finally:
        hooks.clear_hooks()
    assert sorted(got) == ["drop", "pos", "sec"]


# ------------------------------------------------------------------------------------------ wording (SPEC 11.4)


def test_every_template_is_free_of_forbidden_phrases() -> None:
    texts = templates.all_template_strings()
    assert len(texts) >= 20
    for t in texts:
        assert templates.violations_in(t) == [], t


@pytest.mark.parametrize(
    "text",
    [
        "You should buy NVDA now", "you should sell", "This is guaranteed", "guarantee of returns", "You can't lose", "cannot lose",
        "price target of $300", "NVDA will go up", "Act now before it's too late", "Don't miss out", "FOMO is real", "risk-free trade",
        "we recommend selling", "expected return of 12%", "Hurry", "last chance",
    ],
)  # fmt: skip
def test_the_forbidden_phrase_scanner_catches_bad_wording(text: str) -> None:
    assert templates.violations_in(text), text
    with pytest.raises(ValueError, match="wording rules"):
        templates.assert_clean(text)


def test_approved_wording_passes_the_scanner() -> None:
    templates.assert_clean(
        "Your rule max position 15% is exceeded: XYZ is 17.2% of your portfolio. Options: reduce by at least 2.2 pp, raise the cap, or dismiss.",
        "Valuation (P/E) is at the 92nd percentile of its peers; consider reviewing the key risks below.",
        "Matches your long-term growth profile; key risks: concentration.",
    )


def _state(positions: list[PositionState], cash: Decimal, trades: int, rules: Rules) -> PortfolioState:
    return PortfolioState(
        uuid4(),
        uuid4(),
        "Main",
        cash,
        cash,
        tuple(positions),
        trades,
        rules,
        "queue",
        D(0),
        datetime.now(UTC),
    )


@given(
    holdings=st.lists(
        st.tuples(
            st.sampled_from(["AAA", "BBB", "CCC", "DDD", "EEE"]),
            st.sampled_from(["Tech", "Health", "Energy", None]),
            st.integers(1, 5000),
            st.decimals(min_value="1", max_value="900", places=2),
            st.decimals(min_value="1", max_value="900", places=2),
            st.decimals(min_value="-12", max_value="12", places=2),
        ),
        min_size=1,
        max_size=5,
        unique_by=lambda t: t[0],
    ),
    cash=st.decimals(min_value="0", max_value="500000", places=2),
    trades=st.integers(0, 20),
    pos_cap=st.integers(1, 60),
    sec_cap=st.integers(1, 90),
)
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_generated_proposal_text_never_breaks_the_wording_rules(
    holdings: list[Any], cash: Decimal, trades: int, pos_cap: int, sec_cap: int
) -> None:
    positions = []
    for sym, sector, qty, price, cost, chg in holdings:
        prev = price / (1 + chg / 100)
        positions.append(PositionState(sym, f"{sym} Corp.", sector, D(qty), cost, price, prev, None))
    st_ = _state(positions, cash, trades, Rules(D(pos_cap), D(max(sec_cap, 1)), D(15), 10, D(0)))
    cands = service.generate_candidates(st_)  # assert_clean runs inside; re-scan the output independently
    for c in cands:
        for text in (c.title, c.body, *c.rationale, *c.sources):
            assert templates.violations_in(text) == [], text
        assert c.kind in {"trade", "review"} and (c.suggested_qty > 0) == (c.kind == "trade")
        if c.kind == "trade":
            assert (
                c.after_weight_pct is not None
                and c.current_weight_pct is not None
                and c.after_weight_pct <= c.current_weight_pct
            )
    keys = [(c.symbol, c.rule) for c in cands]
    assert len(keys) == len(set(keys))  # at most one candidate per (symbol, rule)


async def test_generated_texts_for_seeded_personas_are_clean(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya", "alex")
    for who in ("priya", "alex"):
        h, _ = await login(tclient, who)
        for p in await proposals(tclient, h):
            for text in (
                p["title"],
                p["body"],
                *p["rationale"],
                *p["sources"],
                p["why"]["summary"],
                *(f["detail"] for f in p["why"]["top_features"]),
            ):
                assert templates.violations_in(text) == [], text


def test_templates_also_pass_the_safety_layer_and_the_disclaimer_is_verbatim() -> None:
    """CR-8: stream B's output guardrail agrees with ours on every template; Appendix A text is identical."""
    from app.modules.safety import DISCLAIMER, check_output

    for t in templates.all_template_strings():
        assert not check_output(t.replace("{", "").replace("}", "")).blocked, t
    assert DISCLAIMER == DISCLAIMER_TEXT
    with pytest.raises(ValueError, match="wording rules"):
        templates.assert_clean("Guaranteed 20% returns")
