"""`recompute_features` end to end on the Postgres test DB (T-PERS-04, interest drift, bias, memories)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from tests.personalization.conftest import NOW

pytestmark = pytest.mark.db


async def test_new_user_cold_start_writes_defaults_and_is_idempotent(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.profile(uid, 3, experience="Beginner", goals=("Income",))
    r1 = await recompute(uid)
    assert r1.n_events == 0 and r1.revealed_tier is None and r1.gap_days == 0
    style = await world.feature(uid, "style_card")
    assert style["depth"] == "standard" and style["jargon"] == 1 and style["metrics"] == ["dividend_yield"]
    assert style["tone"] == "neutral" and style["language"] == "English"
    bias = await world.feature(uid, "bias_flags")
    assert [i["name"] for i in bias["items"]] == [
        "Overtrading",
        "Disposition effect",
        "Concentration",
        "Herding",
        "Loss aversion",
    ]
    assert {i["state"] for i in bias["items"]} == {"ok"}
    assert await world.feature(uid, "features_computed_at") == {"at": NOW.isoformat(), "n_events": 0}
    before = {
        r["key"]: r["value"]
        for r in await world.conn.fetch("select key, value from user_features where user_id = $1", uid)
    }
    await recompute(uid)
    after = {
        r["key"]: r["value"]
        for r in await world.conn.fetch("select key, value from user_features where user_id = $1", uid)
    }
    assert before == after  # idempotent


async def test_user_without_profile_still_recomputes(make_user: Any, world: Any, recompute: Any) -> None:
    uid = await make_user()
    r = await recompute(uid)
    assert r.revealed_tier is None
    assert (await world.feature(uid, "gap_days"))["value"] == 0


async def test_stated_tier_is_never_auto_changed_and_gap_is_flagged(
    make_user: Any, world: Any, recompute: Any
) -> None:
    """T-PERS-04: stated conservative, trades aggressively -> gap flag raised, stated tier untouched."""
    uid = await make_user()
    await world.market(aggressive=True)
    await world.profile(uid, 1, goals=("Preservation",))
    await world.position(uid, "QAT1", 100, 100)
    await world.position(uid, "QAT2", 100, 100)
    since = (NOW - timedelta(days=15)).date()
    await world.set_feature(uid, "gap_days", {"value": 15, "since": since.isoformat(), "flag": False})
    # if the job ever tried to UPDATE/DELETE profiles this would raise and fail the recompute
    await world.conn.execute(
        """
        create or replace function guard_profiles() returns trigger language plpgsql as
        $$ begin raise exception 'profiles must not be written by recompute_features'; end $$
        """
    )
    await world.conn.execute(
        "create trigger guard_profiles_t before update or delete on profiles for each row execute function guard_profiles()"
    )
    try:
        result = await recompute(uid)
    finally:
        await world.conn.execute("drop trigger if exists guard_profiles_t on profiles")
        await world.conn.execute("drop function if exists guard_profiles()")
    assert result.revealed_tier is not None and result.revealed_tier >= 4.0
    revealed = await world.feature(uid, "revealed_tier")
    assert revealed["stated"] == 1 and revealed["gap"] >= 3.0 and revealed["gap_flag"] is True
    gap = await world.feature(uid, "gap_days")
    assert gap["value"] == 15 and gap["flag"] is True and gap["since"] == since.isoformat()
    assert (
        await world.conn.fetchval("select risk_tier_stated from profiles where user_id = $1", uid) == 1
    )  # unchanged


async def test_gap_below_14_days_is_not_flagged_and_closes_when_behaviour_converges(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.market(aggressive=True)
    await world.profile(uid, 1)
    await world.position(uid, "QAT1", 10, 100)
    r = await recompute(uid)
    assert (
        r.gap_days == 0 and (await world.feature(uid, "gap_days"))["flag"] is False
    )  # the clock starts today
    r = await recompute(uid, NOW + timedelta(days=14))
    assert r.gap_days == 14 and (await world.feature(uid, "gap_days"))["flag"] is True
    await world.profile(uid, 5)  # user re-states their tier: gap closes
    r = await recompute(uid, NOW + timedelta(days=15))
    assert r.gap_days == 0 and (await world.feature(uid, "gap_days"))["since"] is None


async def test_conservative_portfolio_has_a_low_revealed_tier(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.market(aggressive=False)
    await world.profile(uid, 2)
    await world.position(uid, "QAD1", 50, 100)
    await world.position(uid, "QAD2", 50, 100)
    r = await recompute(uid)
    assert r.revealed_tier is not None and r.revealed_tier <= 2.0
    assert (await world.feature(uid, "revealed_tier"))["beta"] < 0.7
    assert (await world.feature(uid, "gap_days"))["flag"] is False


async def test_events_drive_interest_style_topics_and_sections(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.profile(uid, 3, experience="Intermediate")
    await world.interest(uid, "factor:dividend", 0.9)
    await world.interest(uid, "sector:Technology", 0.25)
    await world.set_feature(uid, "style_card", {"tone": "Direct", "language": "English", "depth": "standard"})
    for i in range(20):  # T-PERS-03 building block: interest drifts from dividends to tech
        await world.event(
            uid, "ticker_viewed", NOW - timedelta(hours=i), {"ticker": "QAT1" if i % 2 else "QAT2"}
        )
    for i in range(6):
        await world.event(uid, "brief_opened", NOW - timedelta(hours=i), {"ticker": "QAT1"})
    for i in range(24):
        await world.event(
            uid,
            "section_expanded",
            NOW - timedelta(hours=i),
            {"ticker": "QAT1", "section": "trend"},
            {"dwell_ms": 15000},
        )
    await world.event(uid, "chat_query", NOW, {}, {"topics": ["growth", "valuation"]})
    for i in range(3):
        await world.event(uid, "metric_clicked", NOW - timedelta(days=i), {"metric": "revenue_growth"})
    r = await recompute(uid)
    assert r.n_events == 54
    weights = {
        x["dim"]: float(x["weight"])
        for x in await world.conn.fetch(
            "select dim, weight from user_interest_vector where user_id = $1", uid
        )
    }
    assert weights["sector:Technology"] > weights["factor:dividend"]  # top interest flipped within 20 events
    assert weights["factor:growth"] > 0 and weights["factor:value"] > 0  # topics became factors
    baseline = await world.conn.fetchval(
        "select baseline from user_interest_vector where user_id = $1 and dim = 'factor:dividend'", uid
    )
    assert float(baseline) == 0.9  # baseline preserved
    style = await world.feature(uid, "style_card")
    assert style["depth"] == "deep" and style["tone"] == "Direct" and style["metrics"] == ["revenue_growth"]
    assert style["section_order"][0] == "trend"
    assert (await world.feature(uid, "pinned_metrics")) == {"metrics": ["revenue_growth"]}
    assert set(await world.feature(uid, "topic_histogram")) == {"growth", "valuation"}
    assert (await world.feature(uid, "section_beta"))["trend"]["mean"] > 0.9


async def test_bias_detectors_from_the_trade_log_and_portfolio(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.market(aggressive=True)
    await world.profile(uid, 4)
    await world.conn.execute(
        "insert into risk_rules (user_id, rule_key, value) values ($1, 'trades', 10)", uid
    )
    await world.position(uid, "QAT1", 900, 100)  # one dominant position -> concentration
    await world.position(uid, "QAD1", 10, 100)
    for i in range(9):  # 9 trades in the last 7 days against a rule of 10
        await world.fill(uid, "QAT1", "buy", 1, 100.0, NOW - timedelta(hours=6 * i + 1))
    r = await recompute(uid)
    by = {b["name"]: b for b in r.bias}
    assert by["Overtrading"]["state"] == "watch" and by["Overtrading"]["value"] == "9 (rule 10)"
    assert by["Concentration"]["state"] == "flag"
    assert by["Disposition effect"]["value"].startswith("n/a")
    stored = await world.feature(uid, "bias:overtrading")
    assert stored["state"] == "watch" and stored["metric"] == "Trades per week"


async def test_loss_aversion_reads_decided_reduce_proposals(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.market(aggressive=True)
    await world.profile(uid, 4)
    await world.position(uid, "QAT1", 10, 1_000.0)  # cost far above price: a loser
    await world.position(uid, "QAT2", 10, 1.0)  # cost far below price: a winner
    for i in range(4):
        await world.proposal(uid, "QAT1", "rejected", NOW - timedelta(days=2 + i))
        await world.proposal(uid, "QAT2", "approved", NOW - timedelta(days=2 + i))
    await world.proposal(uid, "QAT2", "pending", NOW - timedelta(days=1))  # undecided: ignored
    await world.proposal(
        uid, "QAT2", "rejected", NOW - timedelta(days=1), side="buy"
    )  # not a reduce proposal
    r = await recompute(uid)
    la = next(b for b in r.bias if b["name"] == "Loss aversion")
    assert la["state"] == "flag" and la["raw"] > 2.0 and la["n"] == 8


async def test_memories_are_created_only_from_the_users_own_events_and_consolidate(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.profile(uid, 4)
    for i in range(2):
        await world.event(
            uid,
            "proposal_rejected",
            NOW - timedelta(days=1 + i),
            {"ticker": "QAT1", "proposal_ref": f"p{i}"},
            {"reason": "disagree"},
        )
    for i in range(12):
        await world.event(uid, "ticker_viewed", NOW - timedelta(hours=i), {"ticker": "QAT1"})
    r1 = await recompute(uid)
    assert r1.memories["add"] >= 2
    rows = await world.conn.fetch(
        "select kind, text, source, mem_key, confidence, source_event_ids from memories where user_id = $1 order by mem_key",
        uid,
    )
    keys = {r["mem_key"]: r for r in rows}
    reject = keys["episodic:reject:QAT1"]
    assert reject["text"] == "Rejected 2 proposals for QAT1, most recent reason: disagree with thesis."
    assert reject["kind"] == "episodic" and len(reject["source_event_ids"]) == 2
    assert "semantic:interest:sector:Technology" in keys
    # NOOP on an unchanged log; UPDATE when evidence changes
    r2 = await recompute(uid)
    assert r2.memories["add"] == 0 and r2.memories["update"] == 0 and r2.memories["noop"] >= 2
    await world.event(
        uid, "proposal_rejected", NOW, {"ticker": "QAT1", "proposal_ref": "p9"}, {"reason": "timing"}
    )
    r3 = await recompute(uid)
    assert r3.memories["update"] == 1
    assert (
        await world.conn.fetchval(
            "select text from memories where user_id = $1 and mem_key = 'episodic:reject:QAT1'", uid
        )
        == "Rejected 3 proposals for QAT1, most recent reason: timing."
    )
    assert (
        await world.conn.fetchval(
            "select count(*) from memories where user_id = $1 and mem_key = 'episodic:reject:QAT1'", uid
        )
        == 1
    )


async def test_contradicted_memory_loses_confidence_once_a_day_then_archives(
    make_user: Any, world: Any, recompute: Any
) -> None:
    uid = await make_user()
    await world.profile(uid, 4)
    await world.interest(uid, "sector:Technology", 0.2)  # behaviour/baseline no longer supports the memory
    await world.conn.execute(
        "insert into memories (user_id, kind, text, source, confidence, mem_key) values ($1, 'semantic', 'Shows strong interest in Technology.', 'Views and briefs', 0.5, 'semantic:interest:sector:Technology')",
        uid,
    )
    explicit = await world.conn.fetchval(
        "insert into memories (user_id, kind, text, source, confidence) values ($1, 'semantic', 'Excludes tobacco companies.', 'Onboarding', 1.0) returning id",
        uid,
    )
    await recompute(uid)
    await recompute(uid)  # same day: no second penalty
    conf = float(
        await world.conn.fetchval(
            "select confidence from memories where user_id = $1 and mem_key is not null", uid
        )
    )
    assert conf == pytest.approx(0.3)
    await recompute(uid, NOW + timedelta(days=1))
    row = await world.conn.fetchrow(
        "select confidence, archived from memories where user_id = $1 and mem_key is not null", uid
    )
    assert float(row["confidence"]) == pytest.approx(0.1) and row["archived"] is True
    assert (
        await world.conn.fetchval("select archived from memories where id = $1", explicit) is False
    )  # explicit statements untouched


async def test_recompute_is_tenant_isolated(make_user: Any, world: Any, recompute: Any) -> None:
    a, b = await make_user(), await make_user()
    await world.profile(a, 3)
    await world.profile(b, 3)
    await world.event(b, "ticker_viewed", NOW, {"ticker": "QAT1"})
    await recompute(a)
    assert await world.conn.fetchval("select count(*) from user_features where user_id = $1", b) == 0
    assert await world.conn.fetchval("select count(*) from user_features where user_id = $1", a) > 0
    r = await recompute(a)
    assert r.n_events == 0  # B's event is invisible to A's job
