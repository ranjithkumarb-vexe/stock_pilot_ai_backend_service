"""Synthetic-persona learning curves for the ranker (T-PERS-02, T-PERS-03, T-PERS-05 maths).

The simulator is pure: fresh candidate pools are drawn every step, the persona acts on what the ranker shows in
its top 5, and the bandit posteriors are updated through the same `updated_arms` the service uses. NDCG@5 is
measured on pools the ranker has never seen, against ground-truth relevance defined by the persona.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import mean

import pytest

from app.modules.ranking import metrics
from app.modules.ranking.bandit import cold_start_priors
from app.modules.ranking.models import Arm, Candidate
from app.modules.ranking.scoring import ALPHA_BETA_MAX, rank, updated_arms

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
SECTORS = ["Consumer Staples", "Utilities", "Technology", "Communication", "Energy"]
ARM_TYPES = ["filing", "move", "earnings"]
PRIORS = cold_start_priors(3)


def make_pool(rng: random.Random, n: int = 10) -> list[Candidate]:
    pool = []
    for i in range(n):
        sector = rng.choice(SECTORS)
        arm = rng.choice(ARM_TYPES)
        pool.append(
            Candidate(
                kind="alert", id=f"p{rng.random():.12f}-{i}", arm=arm, materiality=rng.uniform(0.45, 0.65),
                critical=False, title="t", created_at=T0 - timedelta(minutes=i), item={}, symbol=f"S{i}",
                sector=sector,
            )
        )  # fmt: skip
    return pool


Arms = dict[tuple[str, str], Arm]


def learn(
    arms: Arms,
    likes: set[str],
    steps_events: int,
    rng: random.Random,
    *,
    dismiss_p: float = 0.5,
    interests: dict[str, float] | None = None,
) -> int:
    """Persona acts on the top 5 of fresh pools until `steps_events` reward events happened. Opens liked items,
    dismisses disliked ones with probability `dismiss_p`. Returns the number of events consumed."""
    events = 0
    while events < steps_events:
        order = rank(make_pool(rng), arms, interests or {}, rng, page_size=5, priors=PRIORS)
        for s in order[:5]:
            liked = s.cand.sector in likes
            if not liked and rng.random() >= dismiss_p:
                continue  # ignored: no event
            arms.update(updated_arms(arms, PRIORS, s.cand.arm, s.cand.sector, 1.0 if liked else 0.0))
            events += 1
            if events >= steps_events:
                break
    return events


def ndcg_eval(
    arms: Arms, likes: set[str], seed: int, pools: int = 40, interests: dict[str, float] | None = None,
    personalised: bool = True,
) -> float:  # fmt: skip
    rng = random.Random(seed)
    out = []
    for _ in range(pools):
        pool = make_pool(rng)
        order = rank(pool, arms if personalised else {}, interests or {}, rng, page_size=5, priors=PRIORS)
        rel = [1.0 if s.cand.sector in likes else 0.0 for s in order]
        ideal = [1.0 if c.sector in likes else 0.0 for c in pool]
        out.append(metrics.ndcg_at_k(rel, 5, pool_relevances=ideal))
    return mean(out)


def mean_over_seeds(fn: Callable[[int], float], seeds: range) -> float:
    return mean(fn(s) for s in seeds)


# --------------------------------------------------------------------------------- T-PERS-02
def test_dividend_persona_reaches_ndcg5_at_least_0_8_within_30_events() -> None:
    likes = {"Consumer Staples", "Utilities"}  # "clicks only dividend content"
    results, baselines = [], []
    for seed in range(12):
        rng = random.Random(1000 + seed)
        arms: Arms = {}
        before = ndcg_eval(arms, likes, seed=9000 + seed)
        learn(arms, likes, 30, rng)
        results.append(ndcg_eval(arms, likes, seed=9000 + seed))
        baselines.append(before)
    after, base = mean(results), mean(baselines)
    print(f"NDCG@5 cold={base:.3f} after 30 events={after:.3f}")
    assert after >= 0.8, (after, base)
    assert after > base + 0.1  # personalisation is what moved it


def test_without_personalisation_the_same_persona_stays_low() -> None:
    likes = {"Consumer Staples", "Utilities"}
    arms: Arms = {}
    learn(arms, likes, 30, random.Random(5))
    on = ndcg_eval(arms, likes, seed=77)
    off = ndcg_eval(arms, likes, seed=77, personalised=False)
    assert on > off


# --------------------------------------------------------------------------------- T-PERS-03 (bandit channel)
def test_learned_sector_weights_follow_a_drift_from_dividends_to_ai() -> None:
    """The faster channel for the top-interest flip is the events-driven interest vector (tested against the
    database in test_feed_service.py). This is the slower, bandit channel: after 20 events of the new focus the
    ranking is already clearly better for the new taste than before, and keeps recovering."""
    old, new = {"Consumer Staples", "Utilities"}, {"Technology", "Communication"}
    before, after20, after40 = [], [], []
    for seed in range(12):
        rng = random.Random(500 + seed)
        arms: Arms = {}
        learn(arms, old, 40, rng)
        before.append(ndcg_eval(arms, new, 7000 + seed))
        learn(arms, new, 20, rng)
        after20.append(ndcg_eval(arms, new, 7000 + seed))
        learn(arms, new, 20, rng)
        after40.append(ndcg_eval(arms, new, 7000 + seed))
    b, a20, a40 = mean(before), mean(after20), mean(after40)
    print(f"drift NDCG@5 vs new taste: before={b:.3f} +20 events={a20:.3f} +40 events={a40:.3f}")
    assert a20 > b + 0.2
    assert a40 > a20 + 0.1 and a40 >= 0.65


# --------------------------------------------------------------------------------- T-PERS-05 (maths)
def test_bot_rejecting_everything_leaves_bounded_arms_and_a_pinned_critical() -> None:
    arms: Arms = {}
    rng = random.Random(1)
    crit = Candidate("alert", "crit", "breach", 0.95, True, "Position cap breached", T0, {})
    for _ in range(500):
        for arm in ("breach", "proposal", "filing", "move", "earnings", "mirror", "brief"):
            arms.update(updated_arms(arms, PRIORS, arm, "Technology", 0.0))
    assert all(a.alpha <= ALPHA_BETA_MAX and a.beta <= ALPHA_BETA_MAX for a in arms.values())
    order = rank([*make_pool(rng), crit], arms, {}, rng, page_size=5, priors=PRIORS)
    assert order[0].cand.id == "crit" and order[0].pinned
    # the posterior is suppressed but never zero: the epsilon floor keeps every item scoreable
    assert all(s.thompson >= 0.1 for s in order)


def test_cold_start_priors_are_weak_and_tier_shaped() -> None:
    cons, aggr = cold_start_priors(2), cold_start_priors(5)
    assert cons["move"].mean < aggr["move"].mean
    assert all(
        a.alpha + a.beta <= 6 for a in [*cons.values(), *aggr.values()]
    )  # weak: a handful of pseudo-counts
    assert cold_start_priors(None)["filing"] == Arm(2, 2)
    assert cold_start_priors(3, ["Income"])["brief"].mean > 0.5 - 1e-9


# ----------------------------------------------------- CR-20 / T-PERS-TTP: time to personalization <= 30 events
@pytest.mark.parametrize(
    "seeds", [tuple(range(8)), tuple(range(100, 132))], ids=["eval-seeds", "held-out-seeds"]
)
def test_time_to_personalization_is_at_most_30_events_on_eval_and_held_out_seeds(
    seeds: tuple[int, ...],
) -> None:
    """The ranker was tuned on `range(8)`; the held-out replications guard against overfitting that seed list."""
    from evals.persona_sim import run_simulation

    out = run_simulation(seeds=seeds)
    assert out["ranker"] == "app.modules.ranking"
    agg = out["aggregate"]
    assert agg["time_to_personalization_events"] <= 30.0, agg
    # faster learning must not cost quality, calibration or the guardrails
    assert agg["ndcg5_personalised"] >= 0.93 and agg["ndcg5_lift"] >= 0.45 and agg["approve_rate_lift"] > 0
    assert out["bot"]["critical_first_rate"] == 1.0 and out["bot"]["posterior_bounded"]
    assert out["drift_flip_rate_within_20_events"] >= 0.8
    for p in out["personas"]:
        if p["id"] != "P6":
            assert p["personalised"]["brier"] <= p["baseline"]["brier"], p["id"]
