"""Pure ranker maths: score formula, bounded posteriors, epsilon floor, MMR, pinned criticals, metrics."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.modules.ranking import metrics
from app.modules.ranking.models import Arm, Candidate
from app.modules.ranking.scoring import (
    ALPHA_BETA_MAX,
    EPS_FLOOR,
    NEUTRAL_INTEREST,
    PARAM_MIN,
    W_INTEREST,
    W_MATERIALITY,
    W_THOMPSON,
    clamp_arm,
    item_similarity,
    mmr_order,
    rank,
    score_candidates,
    update_arm,
    updated_arms,
)

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


def cand(
    id_: str, arm: str = "filing", mat: float = 0.6, *, critical: bool = False, symbol: str | None = None,
    sector: str | None = None, age_min: int = 10,
) -> Candidate:  # fmt: skip
    return Candidate(
        kind="alert", id=id_, arm=arm, materiality=mat, critical=critical, title=id_,
        created_at=T0 - timedelta(minutes=age_min), item={"id": id_}, symbol=symbol, sector=sector,
    )  # fmt: skip


# --------------------------------------------------------------------------------- posterior bounds
@given(
    st.floats(0.01, 500),
    st.floats(0.01, 500),
)
def test_clamp_arm_is_always_bounded(a: float, b: float) -> None:
    arm = clamp_arm(a, b)
    assert PARAM_MIN <= arm.alpha <= ALPHA_BETA_MAX
    assert PARAM_MIN <= arm.beta <= ALPHA_BETA_MAX
    assert arm.alpha + arm.beta <= ALPHA_BETA_MAX + 1e-9


@given(st.lists(st.sampled_from([0.0, 0.5, 1.0]), min_size=1, max_size=300))
def test_update_arm_never_leaves_the_bounds(rewards: list[float]) -> None:
    arm = Arm(2.0, 2.0)
    for r in rewards:
        arm = update_arm(arm, r)
        assert PARAM_MIN <= arm.alpha <= ALPHA_BETA_MAX and PARAM_MIN <= arm.beta <= ALPHA_BETA_MAX


def test_a_bot_rejecting_everything_cannot_push_a_posterior_past_50() -> None:
    arm = Arm(2.0, 2.0)
    for _ in range(10_000):
        arm = update_arm(arm, 0.0)
    assert arm.beta <= ALPHA_BETA_MAX and arm.alpha >= PARAM_MIN
    assert (
        arm.mean < 0.15
    )  # strongly suppressed (discounted updates settle near prior + 20 pseudo-counts) ...


def test_fractional_reward_moves_both_parameters() -> None:
    arm = update_arm(Arm(2, 2), 0.5)
    assert arm.alpha == pytest.approx(2.5) and arm.beta == pytest.approx(2.5)
    assert update_arm(Arm(2, 2), 7.0).alpha == pytest.approx(3.0)  # reward clipped to [0, 1]


def test_updated_arms_touch_type_and_sector_context() -> None:
    priors = {"filing": Arm(3, 2)}
    out = updated_arms({}, priors, "filing", "Technology", 1.0)
    assert set(out) == {("filing", ""), ("sector", "sector:Technology")}
    assert out[("filing", "")].alpha == pytest.approx(4.0)
    assert out[("sector", "sector:Technology")].alpha == pytest.approx(
        1.0 + 3.0
    )  # Beta(1,1) + 3 pseudo-counts
    assert set(updated_arms({}, priors, "filing", None, 0.0)) == {("filing", "")}


# --------------------------------------------------------------------------------- score
def test_score_is_exactly_the_specified_weighted_sum() -> None:
    c = cand("a", mat=0.8, sector="Technology")
    arms = {("filing", ""): Arm(40, 2), ("sector", "sector:Technology"): Arm(40, 2)}
    [s] = score_candidates([c], arms, {"sector:Technology": 0.5}, random.Random(1))
    assert (W_MATERIALITY, W_THOMPSON, W_INTEREST) == (0.5, 0.3, 0.2)
    expected = 0.5 * 0.8 + 0.3 * s.thompson + 0.2 * 0.5
    assert s.score == pytest.approx(expected, abs=1e-5)
    assert s.interest == 0.5 and s.materiality == 0.8


def test_unknown_interest_dim_is_neutral() -> None:
    [s] = score_candidates([cand("a", sector="Healthcare")], {}, {"sector:Technology": 1.0}, random.Random(1))
    assert s.interest == NEUTRAL_INTEREST
    [s2] = score_candidates([cand("b")], {}, {}, random.Random(1))
    assert s2.interest == NEUTRAL_INTEREST


def test_epsilon_floor_keeps_a_dead_arm_alive() -> None:
    c = cand("a", arm="move")
    dead = {("move", ""): Arm(PARAM_MIN, ALPHA_BETA_MAX)}
    for seed in range(50):
        [s] = score_candidates([c], dead, {}, random.Random(seed))
        assert s.thompson >= EPS_FLOOR


def test_draws_are_deterministic_for_a_seed_and_shared_inside_an_arm() -> None:
    cs = [cand("a", mat=0.5), cand("b", mat=0.6)]
    one = score_candidates(cs, {}, {}, random.Random(7))
    two = score_candidates(cs, {}, {}, random.Random(7))
    assert [s.score for s in one] == [s.score for s in two]
    assert one[0].thompson == one[1].thompson  # same arm -> same draw -> ordered by their own materiality


def test_a_learned_arm_outranks_a_disliked_one_at_equal_materiality() -> None:
    liked, disliked = cand("like", arm="filing"), cand("dis", arm="move")
    arms = {("filing", ""): Arm(40, 2), ("move", ""): Arm(2, 40)}
    wins = 0
    for seed in range(100):
        order = rank([disliked, liked], arms, {}, random.Random(seed))
        wins += order[0].cand.id == "like"
    assert wins >= 95


# --------------------------------------------------------------------------------- pinned criticals
def test_criticals_are_pinned_first_even_when_the_bandit_hates_them() -> None:
    crit = cand("crit", arm="breach", mat=0.2, critical=True)
    others = [cand(f"o{i}", arm="filing", mat=0.99) for i in range(5)]
    arms = {("breach", ""): Arm(PARAM_MIN, ALPHA_BETA_MAX), ("filing", ""): Arm(ALPHA_BETA_MAX - 1, 1)}
    for seed in range(30):
        order = rank([*others, crit], arms, {}, random.Random(seed))
        assert order[0].cand.id == "crit" and order[0].pinned
        assert all(not s.pinned for s in order[1:])


def test_multiple_criticals_sort_by_materiality_then_recency() -> None:
    a = cand("a", mat=0.9, critical=True, age_min=5)
    b = cand("b", mat=0.95, critical=True, age_min=50)
    c = cand("c", mat=0.9, critical=True, age_min=1)
    order = rank([a, b, c], {}, {}, random.Random(0))
    assert [s.cand.id for s in order] == ["b", "c", "a"]


# --------------------------------------------------------------------------------- MMR
def test_item_similarity_ladder() -> None:
    a = cand("a", arm="filing", symbol="X", sector="Technology")
    assert item_similarity(a, cand("b", arm="filing", symbol="X")) == 1.0
    assert item_similarity(a, cand("b", arm="move", symbol="X")) == 0.8
    assert item_similarity(a, cand("b", arm="filing", symbol="Y", sector="Technology")) == 0.5
    assert item_similarity(a, cand("b", arm="filing", symbol="Y", sector="Energy")) == 0.35
    assert item_similarity(a, cand("b", arm="move", symbol="Y", sector="Technology")) == 0.25
    assert item_similarity(a, cand("b", arm="move", symbol="Y", sector="Energy")) == 0.0


def test_mmr_demotes_a_near_duplicate_below_a_different_story() -> None:
    top = cand("top", arm="filing", mat=0.80, symbol="NVDA", sector="Technology")
    dup = cand("dup", arm="filing", mat=0.79, symbol="NVDA", sector="Technology")
    other = cand("other", arm="move", mat=0.72, symbol="KO", sector="Consumer Staples")
    scored = score_candidates([top, dup, other], {}, {}, random.Random(3))
    order = [s.cand.id for s in mmr_order(scored)]
    assert order[0] == "top"
    assert order.index("other") < order.index("dup")


def test_mmr_is_a_permutation() -> None:
    cs = [cand(f"c{i}", mat=0.3 + i / 20, symbol=f"S{i % 3}") for i in range(9)]
    out = mmr_order(score_candidates(cs, {}, {}, random.Random(2)))
    assert sorted(s.cand.id for s in out) == sorted(c.id for c in cs)


# --------------------------------------------------------------------------------- exploration
def test_exploration_surfaces_a_suppressed_arm_item_into_the_visible_page() -> None:
    visible = [cand(f"v{i}", arm="filing", mat=0.9, sector=f"S{i}") for i in range(5)]
    hidden = cand("h", arm="move", mat=0.5)
    arms = {("filing", ""): Arm(45, 2), ("move", ""): Arm(1, 45)}
    shown = 0
    n = 400
    for seed in range(n):
        order = rank([*visible, hidden], arms, {}, random.Random(seed), page_size=5)
        page = [s.cand.id for s in order[:5]]
        shown += "h" in page
    rate = shown / n
    assert 0.03 < rate < 0.2  # about EXPLORE_P = 10 %, never 0 and never dominant


def test_exploration_never_displaces_a_critical() -> None:
    crit = cand("crit", arm="breach", mat=0.9, critical=True)
    visible = [cand(f"v{i}", arm="filing", mat=0.9, sector=f"S{i}") for i in range(4)]
    hidden = cand("h", arm="move", mat=0.5)
    arms = {("filing", ""): Arm(45, 2), ("move", ""): Arm(1, 45)}
    for seed in range(100):
        order = rank([crit, *visible, hidden], arms, {}, random.Random(seed), page_size=5)
        assert order[0].cand.id == "crit"


# --------------------------------------------------------------------------------- metrics
def test_ndcg_mrr_and_diversity() -> None:
    assert metrics.ndcg_at_k([1, 1, 0, 0, 0], 5, pool_relevances=[1, 1, 0, 0, 0, 0]) == pytest.approx(1.0)
    assert metrics.ndcg_at_k([0, 0, 1, 1, 0], 5, pool_relevances=[1, 1, 0, 0]) < 0.75
    assert metrics.ndcg_at_k([0, 0, 0], 5) == 1.0  # nothing relevant to find
    assert metrics.mrr([0, 0, 1]) == pytest.approx(1 / 3) and metrics.mrr([0, 0]) == 0.0
    items = [cand("a", symbol="X"), cand("b", symbol="X"), cand("c", symbol="Y")]
    div = metrics.intra_list_diversity(items, item_similarity)
    assert 0.0 <= div <= 1.0
    assert metrics.intra_list_diversity(items[:1], item_similarity) == 1.0
