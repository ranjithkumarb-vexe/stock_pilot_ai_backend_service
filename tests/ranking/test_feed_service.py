"""Feed pipeline against the database: candidates -> risk/exclusion filters -> score -> pinned -> audit -> why.

T-RISK-02, T-PERS-01 (cold start), T-PERS-03 (interest flip), T-PERS-05 (bot), per-user rate limit,
bandit hook wiring, feedback sync, RLS on `why`.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.clock import FakeClock
from app.core.db import user_tx
from app.core.errors import AppError
from app.modules.paper.context import EngineContext
from app.modules.proposals import hooks
from app.modules.ranking import bandit, candidates, service, why
from app.modules.ranking.models import Arm
from app.modules.ranking.scoring import ALPHA_BETA_MAX
from tests.stream_d import NOW, Env, FixedPrices, engine_ctx

pytestmark = pytest.mark.db


async def basic_world(env: Env, prices: FixedPrices, **user_kw: Any) -> UUID:
    uid = await env.user(**user_kw)
    for s in ("QT1", "QT2", "QS1", "QH1", "QX1"):
        prices.set(s, 100.0, 100.0)
    await env.hold(uid, "QT1", 50, 90)
    return uid


async def feed(uid: UUID, ctx: EngineContext, **kw: Any) -> service.FeedResult:
    async with user_tx(uid) as conn:
        return await service.build_feed(conn, uid, ctx, NOW, **kw)


def ids(res: service.FeedResult) -> list[str]:
    return [i["id"] for i in res.items]


# --------------------------------------------------------------------------------- shape / pinning / audit
async def test_feed_pins_criticals_first_and_includes_a_code_built_briefing(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    crit = await env.alert(
        uid, type="breach", symbol="QT1", materiality=0.5, critical=True, title="Position cap breached"
    )
    a1 = await env.alert(uid, type="filing", symbol="QS1", materiality=0.9)
    a2 = await env.alert(uid, type="move", symbol="QH1", materiality=0.4)
    res = await feed(uid, ctx)
    kinds = [(i["kind"], i["pinned"]) for i in res.items]
    assert res.items[0]["id"] == str(crit) and res.items[0]["pinned"] is True
    assert [k for k in kinds if k[1]] == [("alert", True)]  # only the critical is pinned
    assert {str(a1), str(a2)} <= set(ids(res))
    brief = next(i for i in res.items if i["kind"] == "briefing" and i["item"]["title"] == "Daily briefing")
    text = " ".join(b["text"] for b in brief["item"]["bullets"])
    assert "$" in text and "rule" in text.lower()  # numbers and rules come from the ledger
    for it in res.items:  # contract row shape
        assert set(it) == {"kind", "id", "pinned", "score", "item", "why"}
        assert set(it["why"]) == {"summary", "top_features", "rules_checked", "sources", "basis"}
        assert 0 <= it["score"] <= 1.2


async def test_one_audit_row_per_ranked_item_and_reload_does_not_duplicate_it(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    await env.alert(uid, type="filing", symbol="QS1", materiality=0.7)
    await env.alert(uid, type="move", symbol="QH1", materiality=0.5)
    res = await feed(uid, ctx)
    n = await env.c.fetchval("select count(*) from personalization_audit where user_id = $1", uid)
    assert n == len(res.items)
    await feed(uid, ctx)  # same 5-minute draw: nothing new to record
    assert await env.c.fetchval("select count(*) from personalization_audit where user_id = $1", uid) == n
    row = await env.c.fetchrow(
        "select * from personalization_audit where user_id = $1 and output_type = 'alert' limit 1", uid
    )
    assert row["basis"] in ("profile", "behaviour", "cold_start")
    assert {"materiality", "learned", "interest"} <= set(row["features_used"]) or row["features_used"][
        "pinned"
    ]
    assert row["arm_scores"]["score"] >= 0 and row["rules_checked"]


async def test_cold_start_says_based_on_your_setup_and_uses_persona_priors(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    """T-PERS-01: questionnaire only -> persona-prior ranking, 'why' says 'based on your setup'."""
    uid = await basic_world(env, prices, tier=2)
    a = await env.alert(uid, type="filing", symbol="QS1", materiality=0.7)
    await env.event(uid, "onboarding_complete")
    res = await feed(uid, ctx)
    item = next(i for i in res.items if i["id"] == str(a))
    assert item["why"]["basis"] == "cold_start"
    assert item["why"]["summary"].startswith("Based on your setup")
    assert (
        "nothing learned yet"
        in item["why"]["top_features"][0]["detail"]
        + item["why"]["top_features"][1]["detail"]
        + item["why"]["top_features"][-1]["detail"]
    )
    assert (
        await env.c.fetchval("select count(*) from bandit_arms where user_id = $1", uid) == 0
    )  # priors only


async def test_basis_turns_to_behaviour_once_the_user_has_acted(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices, tier=3)
    a = await env.alert(uid, type="filing", symbol="QS1", materiality=0.7)
    for _ in range(6):
        await env.event(uid, "ticker_viewed", entity={"ticker": "QS1"})
    await env.arm(uid, "filing", 30, 3)
    res = await feed(uid, ctx)
    item = next(i for i in res.items if i["id"] == str(a))
    assert item["why"]["basis"] == "behaviour"
    assert item["why"]["summary"].startswith("Score ")
    assert len(item["why"]["top_features"]) == 3
    weights = [f["weight"] for f in item["why"]["top_features"]]
    assert weights == sorted(weights, reverse=True)


async def test_feed_paging_cursor_and_limit(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    for i in range(8):
        await env.alert(uid, type="move", symbol="QS1", materiality=0.5 + i / 100)
    first = await feed(uid, ctx, limit=4)
    assert len(first.items) == 4 and first.next_cursor == "4" and first.total >= 9
    second = await feed(uid, ctx, limit=4, offset=int(first.next_cursor))
    assert set(ids(first)).isdisjoint(ids(second))


async def test_dismissed_snoozed_and_old_alerts_are_not_candidates_but_expired_snooze_returns(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    dis = await env.alert(uid, state="dismissed", materiality=0.9)
    old = await env.alert(uid, age_min=60 * 24 * 20, materiality=0.9)
    snoozed = await env.alert(uid, state="snoozed", materiality=0.9)
    due = await env.alert(uid, state="snoozed", materiality=0.9)
    await env.c.execute(
        "update alerts set snoozed_until = $2 where id = $1", snoozed, NOW + timedelta(hours=5)
    )
    await env.c.execute("update alerts set snoozed_until = $2 where id = $1", due, NOW - timedelta(minutes=1))
    got = set(ids(await feed(uid, ctx)))
    assert str(due) in got
    assert not ({str(dis), str(old), str(snoozed)} & got)


# --------------------------------------------------------------------------------- T-RISK-02
async def test_a_buy_that_would_push_a_position_to_16_percent_is_removed_before_ranking(
    env: Env, db: Any, prices: FixedPrices
) -> None:
    """T-RISK-02: 15% cap; the bandit's top item would push QT1 to 16% -> removed before ranking, never shown."""
    uid = await env.user(cash=85_500, rules={"pos": 15})
    prices.set("QT1", 100.0, 100.0)
    prices.set("QS1", 100.0, 100.0)
    await env.hold(uid, "QT1", 145, 100)  # 14.5 % of a 100,000 portfolio
    bad = await env.proposal(uid, symbol="QT1", side="buy", qty=15, rule="pos")  # -> 16.0 %
    fine = await env.proposal(uid, symbol="QT1", side="buy", qty=5, rule="pos2")  # -> 15.0 % (not over)
    sell = await env.proposal(uid, symbol="QT1", side="sell", qty=10, rule="pos3")
    await env.arm(uid, "proposal", 50, 1)  # the bandit loves proposals
    await env.arm(uid, "sector", 50, 1, "sector:Technology")
    ctx = engine_ctx(prices)
    async with user_tx(uid) as conn:
        ranked, dropped, _ = await service.rank_candidates(conn, uid, ctx, NOW)
        res = await service.build_feed(conn, uid, ctx, NOW)
    shown = {s.cand.id for s in ranked}
    assert str(bad) not in shown and str(bad) not in ids(res)
    reasons = {c.id: r for c, r in dropped}
    assert reasons[str(bad)] == "risk:pos"
    assert {str(fine), str(sell)} <= shown
    top_ids = [s.cand.id for s in ranked if s.cand.kind == "proposal"]
    assert top_ids  # the bandit-favoured proposals that ARE allowed are still ranked


async def test_validate_can_also_veto_on_cash_and_unknown_price(
    env: Env, db: Any, prices: FixedPrices
) -> None:
    uid = await env.user(cash=500)
    prices.set("QT1", 100.0, 100.0)
    big = await env.proposal(uid, symbol="QT1", side="buy", qty=50)  # cannot afford
    nop = await env.proposal(uid, symbol="QS1", side="buy", qty=1)  # no quote for QS1
    ctx = engine_ctx(prices)
    async with user_tx(uid) as conn:
        _, dropped, _ = await service.rank_candidates(conn, uid, ctx, NOW)
    reasons = {c.id: r for c, r in dropped}
    assert reasons[str(big)].startswith("risk:")
    assert reasons[str(nop)] in ("risk:no_price", "risk:funds", "risk:cash") or reasons[str(nop)].startswith(
        "risk:"
    )


async def test_exclusion_lists_drop_non_held_items_but_never_criticals_or_holdings(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices, esg=("Tobacco",))
    await env.c.execute("insert into sector_prefs (user_id, sector, pref) values ($1, 'Healthcare', -1)", uid)
    tob = await env.alert(uid, symbol="QX1", materiality=0.99)
    health = await env.alert(uid, symbol="QH1", materiality=0.99)
    crit = await env.alert(uid, type="breach", symbol="QX1", materiality=0.9, critical=True)
    tech = await env.alert(uid, symbol="QT2", materiality=0.5)
    got = set(ids(await feed(uid, ctx)))
    assert str(tob) not in got and str(health) not in got
    assert str(crit) in got and str(tech) in got
    await env.hold(uid, "QX1", 10, 100)  # now held: its news matters again
    assert str(tob) in set(ids(await feed(uid, ctx)))


def test_excluded_by_matching_rules() -> None:
    assert candidates.excluded_by(["Tobacco"], set(), "Consumer Staples", "Tobacco") == "Tobacco"
    assert candidates.excluded_by(["Fossil fuels"], set(), "Energy", "Oil & Gas") == "Fossil fuels"
    assert candidates.excluded_by([], {"Energy"}, "Energy", None) == "sector:Energy"
    assert candidates.excluded_by(["Tobacco"], set(), "Technology", "Software") is None


# --------------------------------------------------------------------------------- bandit wiring
async def test_proposal_decisions_update_the_bandit_through_the_registered_hook(
    env: Env, db: Any, prices: FixedPrices
) -> None:
    uid = await basic_world(env, prices)
    bandit.register_hooks()
    bandit.register_hooks()  # idempotent
    async with user_tx(uid) as conn:
        await hooks.on_decision(conn, uid, {"symbol": "QT1", "decided_at": NOW.isoformat()}, 0.0)
        await hooks.on_decision(conn, uid, {"symbol": "QT1", "decided_at": NOW.isoformat()}, 0.0)
        await hooks.on_decision(conn, uid, {"symbol": "QT1", "decided_at": NOW.isoformat()}, 0.5)
        arms = await bandit.load_arms(conn, uid)
    prior = bandit.cold_start_priors(3)["proposal"]
    a = arms[("proposal", "")]
    assert a.beta > prior.beta and a.alpha > prior.alpha - 1e-9
    assert ("sector", "sector:Technology") in arms
    assert arms[("sector", "sector:Technology")].beta > 2.0


async def test_unknown_arm_is_ignored(env: Env, db: Any) -> None:
    uid = await env.user()
    async with user_tx(uid) as conn:
        assert await bandit.apply_reward(conn, uid, "nonsense", 1.0, now=NOW) is False
        assert await bandit.load_arms(conn, uid) == {}


async def test_bot_rejecting_everything_priors_bounded_limiter_trips_criticals_still_shown(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    """T-PERS-05."""
    uid = await basic_world(env, prices)
    crit = await env.alert(
        uid, type="breach", symbol="QT1", materiality=0.9, critical=True, title="Position cap breached"
    )
    other = await env.alert(uid, type="filing", symbol="QS1", materiality=0.6)
    clock = FakeClock(start=NOW)
    limiter = bandit.RewardLimiter(burst=20, per_s=1 / 3, monotonic=clock.monotonic)
    prev = bandit.set_limiter(limiter)
    try:
        applied = 0
        async with user_tx(uid) as conn:
            for _ in range(300):  # a bot hammering "reject", no time passing
                applied += await bandit.apply_reward(conn, uid, "proposal", 0.0, sector="Technology", now=NOW)
            arms = await bandit.load_arms(conn, uid)
        assert applied == 20 and limiter.tripped == 280  # the rate limiter trips
        for a in arms.values():
            assert a.alpha <= ALPHA_BETA_MAX and a.beta <= ALPHA_BETA_MAX  # priors bounded
        clock.advance(30)  # tokens refill: updates flow again, slowly
        async with user_tx(uid) as conn:
            assert await bandit.apply_reward(conn, uid, "proposal", 0.0, now=NOW)
    finally:
        bandit.set_limiter(prev)
    # even with every learned arm crushed, the critical alert is still shown, first and pinned
    async with user_tx(uid) as conn:
        for arm in ("breach", "filing", "move", "earnings", "mirror", "brief"):
            await bandit._write(conn, uid, arm, "", Arm(0.1, 49.9), NOW)
    res = await feed(uid, ctx)
    assert res.items[0]["id"] == str(crit) and res.items[0]["pinned"]
    assert str(other) in ids(res)  # suppressed but not removed


async def test_thumbs_feedback_events_feed_the_bandit_once(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    a = await env.alert(uid, type="move", symbol="QS1", materiality=0.6)
    await env.event(uid, "thumbs_down", entity={"alert_id": str(a)}, props={"action": "feed_alert"})
    await env.event(uid, "thumbs_down", entity={"alert_id": str(a)}, props={"action": "feed_alert"})
    await env.event(
        uid, "thumbs_up", entity={"section": "risk"}, props={"action": "brief_section"}
    )  # not the feed
    await env.event(uid, "thumbs_up", entity={}, props={"action": "feed_briefing"})
    await env.event(uid, "thumbs_up", entity={"alert_id": "not-a-uuid"}, props={"action": "feed_alert"})
    async with user_tx(uid) as conn:
        n = await service.sync_feedback(conn, uid, NOW + timedelta(seconds=1))
        again = await service.sync_feedback(conn, uid, NOW + timedelta(seconds=2))
        arms = await bandit.load_arms(conn, uid)
    assert n == 3 and again == 0
    assert arms[("move", "")].beta > bandit.cold_start_priors(3)["move"].beta + 1
    assert arms[("brief", "")].alpha > 2.0
    assert ("sector", "sector:Consumer Staples") in arms


# --------------------------------------------------------------------------------- why
async def test_why_comes_from_the_audit_row_and_ranks_unseen_items_on_demand(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    a = await env.alert(
        uid, type="filing", symbol="QS1", materiality=0.7, payload={"source_label": "SEC EDGAR 8-K"}
    )
    p = await env.proposal(uid, symbol="QT1", side="sell", qty=5)
    async with user_tx(uid) as conn:
        w = await service.explain(conn, uid, ctx, NOW, "alert", str(a))  # never ranked before: ranked now
        wp = await service.explain(conn, uid, ctx, NOW, "proposal", str(p))
    assert w["sources"] == ["SEC EDGAR 8-K"] and w["basis"] in ("cold_start", "profile", "behaviour")
    assert wp["rules_checked"]
    assert (
        await env.c.fetchval(
            "select count(*) from personalization_audit where user_id=$1 and output_id=$2", uid, str(a)
        )
        == 1
    )


async def test_why_for_a_critical_and_for_a_dismissed_alert(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    crit = await env.alert(uid, type="breach", symbol="QT1", materiality=0.9, critical=True)
    dis = await env.alert(uid, type="move", symbol="QS1", state="dismissed", materiality=0.5)
    async with user_tx(uid) as conn:
        wc = await service.explain(conn, uid, ctx, NOW, "alert", str(crit))
        wd = await service.explain(conn, uid, ctx, NOW, "alert", str(dis))
    assert wc["summary"].startswith("Pinned") and wc["basis"] == "profile"
    assert wc["top_features"][0]["name"] == "rule_breach"
    assert wd["top_features"]  # explained even though no longer on the live feed


async def test_why_is_404_for_unknown_foreign_and_malformed_ids(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    mine = await basic_world(env, prices)
    theirs = await env.user()
    foreign = await env.alert(theirs, symbol="QS1")
    async with user_tx(mine) as conn:
        for otype, oid in [
            ("alert", str(uuid4())),
            ("alert", str(foreign)),
            ("alert", "nope"),
            ("proposal", str(uuid4())),
            ("brief", str(uuid4())),
            ("brief", "x"),
            ("briefing", str(uuid4())),
        ]:
            with pytest.raises(AppError) as exc:
                await service.explain(conn, mine, ctx, NOW, otype, oid)
            assert exc.value.status_code == 404, (otype, oid)
        with pytest.raises(AppError):
            await service.explain(conn, mine, ctx, NOW, "mystery", str(uuid4()))


async def test_why_for_the_daily_briefing_and_a_brief(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await basic_world(env, prices)
    async with user_tx(uid) as conn:
        res = await service.build_feed(conn, uid, ctx, NOW)
        bid = next(i["id"] for i in res.items if i["item"]["title"] == "Daily briefing")
        w = await service.explain(conn, uid, ctx, NOW, "briefing", bid)
    assert w["rules_checked"] and w["sources"]
    brief_id = await env.c.fetchval(
        "insert into briefs (user_id, symbol, as_of, style_hash) values ($1, 'QT1', $2, 'h') returning id",
        uid,
        NOW.date(),
    )
    await env.c.execute(
        "insert into user_features (user_id, key, value) values ($1, 'style_card', $2)", uid,
        {"order": ["bal", "risk", "moat"], "depth": "deep", "jargon": 2},
    )  # fmt: skip
    async with user_tx(uid) as conn:
        wb = await service.explain(conn, uid, ctx, NOW, "brief", str(brief_id))
        await service.explain(conn, uid, ctx, NOW, "brief", str(brief_id))  # second call reuses the row
    assert wb["basis"] == "cold_start" and wb["summary"].startswith("Based on your setup")
    assert [f["name"] for f in wb["top_features"]] == ["section_order", "depth", "jargon_level"]
    assert (
        await env.c.fetchval("select count(*) from personalization_audit where output_id = $1", str(brief_id))
        == 1
    )


# --------------------------------------------------------------------------------- T-PERS-03 (interest vector channel)
async def test_interest_flip_within_20_events_and_the_ranking_follows(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    """Dividends -> AI: after 20 events on the new focus the top interest flips (and the drift flag is raised),
    and the ranker's interest term makes the Technology item outrank the Staples item of equal materiality."""
    from app.modules.personalization.recompute import recompute_features

    uid = await basic_world(env, prices)
    await env.c.execute("update bandit_arms set alpha = alpha where false")
    await env.interest(uid, "sector:Consumer Staples", 0.35)
    await env.interest(uid, "sector:Technology", 0.15)
    for i in range(6):
        await env.event(uid, "ticker_viewed", NOW - timedelta(days=14, hours=i), {"ticker": "QS1"})
    async with user_tx(uid) as conn:
        await recompute_features(uid, now=NOW, conn=conn)
        before = {
            r["dim"]: float(r["weight"])
            for r in await conn.fetch("select dim, weight from user_interest_vector where user_id=$1", uid)
        }
    assert max(before, key=lambda d: before[d]) == "sector:Consumer Staples"
    for i in range(20):  # 20 events on the new focus
        await env.event(uid, "ticker_viewed", NOW - timedelta(hours=i), {"ticker": "QT1"})
    async with user_tx(uid) as conn:
        await recompute_features(uid, now=NOW, conn=conn)
        after = {
            r["dim"]: float(r["weight"])
            for r in await conn.fetch("select dim, weight from user_interest_vector where user_id=$1", uid)
        }
        drift = await conn.fetchval(
            "select value from user_features where user_id = $1 and key = 'interest_drift'", uid
        )
    assert max(after, key=lambda d: after[d]) == "sector:Technology"
    assert drift["flag"] is True  # the "has your focus changed?" prompt
    s_alert = await env.alert(uid, symbol="QS1", materiality=0.6, title="staples")
    t_alert = await env.alert(uid, symbol="QT2", materiality=0.6, title="tech")
    for ctx_name in (
        "sector:Consumer Staples",
        "sector:Technology",
    ):  # equal, well-known sector posteriors: only interest differs
        await env.arm(uid, "sector", 20, 20, ctx_name)
    await env.arm(uid, "filing", 20, 20)
    async with user_tx(uid) as conn:
        ranked, _, _ = await service.rank_candidates(conn, uid, ctx, NOW, rng=random.Random(4))
    order = [s.cand.id for s in ranked if s.cand.kind == "alert"]
    assert order.index(str(t_alert)) < order.index(str(s_alert))


def test_briefing_ids_are_deterministic_per_user_and_day() -> None:
    from datetime import date

    from app.modules.ranking.briefing import brief_ready_id, briefing_id

    u = uuid4()
    assert briefing_id(u, date(2026, 10, 30)) == briefing_id(u, date(2026, 10, 30))
    assert briefing_id(u, date(2026, 10, 30)) != briefing_id(u, date(2026, 10, 31))
    assert brief_ready_id(u, "AAPL") != brief_ready_id(u, "MSFT")


async def test_empty_user_without_a_portfolio_gets_an_empty_feed(
    env: Env, db: Any, make_user: Any, ctx: EngineContext
) -> None:
    uid = await make_user()
    res = await feed(uid, ctx)
    assert res.items == [] and res.total == 0 and res.next_cursor is None


async def test_user_with_only_cash_gets_a_briefing_with_the_cash_line(
    env: Env, db: Any, prices: FixedPrices, ctx: EngineContext
) -> None:
    uid = await env.user(cash=25_000)
    await env.watch(uid, "QS1")
    await env.watch(uid, "QH1")
    res = await feed(uid, ctx)
    items = [i for i in res.items if i["kind"] == "briefing"]
    titles = [i["item"]["title"] for i in items]
    assert "Daily briefing" in titles and any(t.startswith("Research brief:") for t in titles)
    daily = next(i for i in items if i["item"]["title"] == "Daily briefing")
    assert "$25,000" in daily["item"]["bullets"][0]["text"]
    assert daily["item"]["headline"] == "Your workspace is ready."
    assert why  # module import smoke
