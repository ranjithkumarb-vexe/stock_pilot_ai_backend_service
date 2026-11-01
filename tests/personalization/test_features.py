"""Pure feature maths (SPEC 10.3 B/E/F)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.modules.personalization import features as f

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
SECTORS = {"JNJ": "Healthcare", "KO": "Consumer Staples", "NVDA": "Technology", "AMD": "Technology"}


def E(etype: str, days_ago: float = 0.0, i: int = 0, **kw: Any) -> f.EventRow:
    return f.EventRow(
        f"00000000-0000-0000-0000-{i:012d}",
        NOW - timedelta(days=days_ago),
        etype,
        kw.get("entity", {}),
        kw.get("props", {}),
    )


def test_decay_half_life() -> None:
    assert f.decay(0) == 1.0
    assert f.decay(14) == pytest.approx(0.5)
    assert f.decay(28) == pytest.approx(0.25)
    assert f.decay(-3) == 1.0  # future events do not amplify


def test_interest_views_add_decayed_weight_to_the_sector() -> None:
    events = [
        E("ticker_viewed", 0, 1, entity={"ticker": "NVDA"}),
        E("ticker_viewed", 14, 2, entity={"ticker": "AMD"}),
    ]
    c = f.interest_contributions(events, SECTORS, NOW)
    assert c == {"sector:Technology": pytest.approx(0.10 + 0.05)}  # 0.1 + 0.1 * 0.5
    assert f.interest_contributions([E("ticker_viewed", 0, 1, entity={"ticker": "ZZZ"})], SECTORS, NOW) == {}


def test_interest_topics_map_to_factors_and_weights_are_clamped_to_unit_interval() -> None:
    events = [E("chat_query", 0, 1, props={"topics": ["dividends", "weather"]})]
    assert f.interest_contributions(events, SECTORS, NOW) == {"factor:dividend": pytest.approx(0.05)}
    merged = f.merge_interest(
        {"sector:Technology": 0.9, "factor:value": 0.45}, {"sector:Technology": 5.0, "sector:Energy": 0.2}
    )
    assert merged == {"factor:value": 0.45, "sector:Energy": 0.2, "sector:Technology": 1.0}


def test_interest_flips_after_twenty_events() -> None:
    """T-PERS-03 building block: dividends -> AI/tech. Twenty tech views overtake a 0.9 dividend baseline."""
    baseline = {"factor:dividend": 0.90, "sector:Technology": 0.25}
    events = [E("ticker_viewed", 0.01 * i, i, entity={"ticker": "NVDA"}) for i in range(20)]
    w = f.merge_interest(baseline, f.interest_contributions(events, SECTORS, NOW))
    assert w["sector:Technology"] == 1.0 > w["factor:dividend"]
    few = f.merge_interest(baseline, f.interest_contributions(events[:3], SECTORS, NOW))
    assert few["sector:Technology"] < few["factor:dividend"]


def test_interest_drift_kl_flags_a_focus_change() -> None:
    old = [E("ticker_viewed", 10 + 0.1 * i, i, entity={"ticker": "KO"}) for i in range(10)]
    new = [E("ticker_viewed", 0.1 * i, 100 + i, entity={"ticker": "NVDA"}) for i in range(10)]
    drift = f.interest_drift(old + new, SECTORS, NOW)
    assert drift["flag"] is True and drift["kl"] > 0.3 and drift["enough_data"]
    same = f.interest_drift(
        old + [E("ticker_viewed", 0.1 * i, 200 + i, entity={"ticker": "KO"}) for i in range(10)], SECTORS, NOW
    )
    assert same["flag"] is False and same["kl"] < 0.1
    assert f.interest_drift([], SECTORS, NOW) == {"kl": 0.0, "flag": False, "enough_data": False}


def test_topic_histogram_is_decayed_and_normalised() -> None:
    events = [
        E("chat_query", 0, 1, props={"topics": ["debt"]}),
        E("chat_query", 0, 2, props={"topics": ["debt", "risks"]}),
        E("chat_query", 14, 3, props={"topics": ["growth"]}),
    ]
    h = f.topic_histogram(events, NOW)
    assert sum(h.values()) == pytest.approx(1.0, abs=1e-3) and next(iter(h)) == "debt"
    assert h["growth"] < h["risks"] + 1e-9 or h["growth"] == pytest.approx(0.5 / 3.5, abs=1e-3)
    assert f.topic_histogram([], NOW) == {}


def test_section_beta_engaged_vs_skimmed_and_ordering() -> None:
    ev: list[f.EventRow] = []
    for i in range(6):
        ev.append(E("section_dwell", 0, i, entity={"section": "bal"}, props={"dwell_ms": 12_000}))  # engaged
        ev.append(
            E("section_dwell", 0, 100 + i, entity={"section": "cat"}, props={"dwell_ms": 900})
        )  # skimmed
    ev.append(E("section_dwell", 0, 300, entity={"section": "val"}, props={"dwell_ms": 5_000}))  # neither
    ev.append(E("section_expanded", 0, 301, entity={"section": "risk"}, props={}))
    ev.append(E("section_expanded", 0, 302, entity={"section": "moat"}, props={"dwell_ms": 500}))
    b = f.section_beta(ev, NOW)
    assert b["bal"]["alpha"] == pytest.approx(7.0) and b["bal"]["beta"] == 1.0
    assert b["cat"]["alpha"] == 1.0 and b["cat"]["beta"] == pytest.approx(7.0)
    assert "val" not in b  # 2-8 s is neutral
    assert b["risk"]["mean"] > 0.5 > b["moat"]["mean"]
    order = f.section_order(b)
    assert order[0] == "bal" and order[-1] == "cat" and sorted(order) == sorted(f.DEFAULT_SECTION_ORDER)


def test_section_order_cold_start_keeps_the_fallback() -> None:
    assert f.section_order({}) == list(f.DEFAULT_SECTION_ORDER)
    seeded = ["bal", "risk", "moat", "val", "trend", "cat", "bb"]
    assert f.section_order({}, seeded) == seeded
    weak = f.section_beta([E("section_expanded", 0, 1, entity={"section": "cat"})], NOW)
    assert f.section_order(weak, seeded) == seeded  # one data point is not enough evidence


def test_two_personas_get_different_section_orders() -> None:
    """T-PERS-07 building block: >= 30% of positions differ between a dividend reader and a growth reader."""
    priya = f.section_beta(
        [E("section_dwell", 0, i, entity={"section": "bal"}, props={"dwell_ms": 15_000}) for i in range(5)]
        + [
            E("section_dwell", 0, 50 + i, entity={"section": "risk"}, props={"dwell_ms": 15_000})
            for i in range(5)
        ],
        NOW,
    )
    alex = f.section_beta(
        [E("section_dwell", 0, i, entity={"section": "trend"}, props={"dwell_ms": 15_000}) for i in range(5)]
        + [
            E("section_dwell", 0, 50 + i, entity={"section": "cat"}, props={"dwell_ms": 15_000})
            for i in range(5)
        ],
        NOW,
    )
    a, b = f.section_order(priya), f.section_order(alex)
    assert sum(x != y for x, y in zip(a, b, strict=True)) / len(a) >= 0.3


def test_pinned_metric_needs_three_clicks_in_seven_days() -> None:
    clicks = [E("metric_clicked", d, i, entity={"metric": "pe"}) for i, d in enumerate([0, 1, 6.5])]
    clicks += [E("metric_clicked", 2, 10 + i, entity={"metric": "roic"}) for i in range(2)]
    clicks += [E("metric_clicked", 8, 20 + i, entity={"metric": "fcf_yield"}) for i in range(5)]  # too old
    assert f.pinned_metrics(clicks, NOW) == ["pe"]


def _card(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "existing": None,
        "experience": None,
        "goals": [],
        "events": [],
        "betas": {},
        "pinned": [],
        "now": NOW,
    }
    args.update(kw)
    return f.derive_style_card(**args)


def test_style_card_cold_start_uses_experience_goals_and_defaults() -> None:
    c = _card(experience="Beginner", goals=["Income"])
    assert (c["depth"], c["jargon"], c["length"], c["tone"], c["language"]) == (
        "standard",
        1,
        "medium",
        "neutral",
        "English",
    )
    assert c["metrics"] == ["dividend_yield"] and c["quant_vs_narrative"] == 0.5
    assert c["section_order"] == list(f.DEFAULT_SECTION_ORDER)
    assert _card(experience="Advanced", goals=["Growth"])["jargon"] == 3
    assert _card(goals=[])["metrics"] == ["pe"]


def test_style_card_never_overwrites_explicit_tone_language_and_keeps_seed_values() -> None:
    existing = {
        "tone": "Plain language",
        "language": "Hindi",
        "depth": "deep",
        "metric": "Dividend yield",
        "section_order": ["bal", "risk", "moat", "val", "trend", "cat", "bb"],
    }
    c = _card(existing=existing, experience="Beginner")
    assert c["tone"] == "Plain language" and c["language"] == "Hindi"
    assert c["depth"] == "deep"  # no behavioural evidence yet: the stored value stays
    assert c["metrics"] == ["Dividend yield"] and c["section_order"][0] == "bal"
    assert c["order"] == c["section_order"]  # stream B reads `order` (CR-5)
    only_b = _card(
        existing={"order": ["risk", "bal"]}
    )  # a card written by B's defaults is respected on cold start
    assert only_b["order"][:2] == ["risk", "bal"] and sorted(only_b["order"]) == sorted(
        f.DEFAULT_SECTION_ORDER
    )


def test_style_card_depth_follows_expansion_rate_and_is_idempotent() -> None:
    briefs = [E("brief_opened", 1, i, entity={"ticker": "NVDA"}) for i in range(6)]
    heavy = briefs + [E("section_expanded", 1, 100 + i, entity={"section": "bal"}) for i in range(24)]
    light = [*briefs, E("section_expanded", 1, 100, entity={"section": "bal"})]
    assert _card(events=heavy)["depth"] == "deep"
    assert _card(events=light)["depth"] == "tldr"
    first = _card(events=heavy, experience="Intermediate")
    assert _card(events=heavy, experience="Intermediate", existing=first) == first  # no compounding drift


def test_style_card_jargon_drops_for_glossary_heavy_users_without_compounding() -> None:
    ev = [E("brief_opened", 1, i, entity={"ticker": "KO"}) for i in range(5)] + [
        E("glossary_clicked", 1, 50 + i) for i in range(6)
    ]
    c1 = _card(events=ev, experience="Intermediate")
    assert c1["jargon"] == 1
    c2 = _card(events=ev, experience="Intermediate", existing=c1)
    assert c2["jargon"] == 1 and c2 == c1


def test_style_card_length_from_median_dwell_and_quant_vs_narrative() -> None:
    long = [
        E("section_dwell", 1, i, entity={"section": "bal"}, props={"dwell_ms": 20_000}) for i in range(10)
    ]
    short = [
        E("section_dwell", 1, i, entity={"section": "bal"}, props={"dwell_ms": 1_500}) for i in range(10)
    ]
    assert _card(events=long)["length"] == "long" and _card(events=short)["length"] == "short"
    assert _card(events=long[:9])["length"] == "medium"  # too few dwell samples
    quant = [E("metric_clicked", 1, i, entity={"metric": "pe"}) for i in range(10)]
    narr = [E("section_expanded", 1, i, entity={"section": "bal"}) for i in range(10)]
    assert _card(events=quant)["quant_vs_narrative"] > 0.7 > 0.3 > _card(events=narr)["quant_vs_narrative"]
    assert _card(events=quant, pinned=["pe", "roic"])["metrics"] == ["pe", "roic"]


def test_style_card_validates_against_stream_bs_model_and_cold_start_follows_its_default() -> None:
    """CR-5: the brief generator reads `order/jargon/depth/tone/metric` from this card."""
    from app.modules.research.brief.style import CONSERVATIVE_ORDER, StyleCard, default_style_from_profile

    default = default_style_from_profile("Beginner", ["Income"]).order
    card = _card(experience="Beginner", goals=["Income"], default_order=default)
    parsed = StyleCard.model_validate(card)
    assert parsed.order == CONSERVATIVE_ORDER and parsed.jargon == 1 and parsed.depth == "standard"
    assert parsed.metric == "dividend_yield"


def test_style_card_explicit_user_choices_win_over_derived_values() -> None:
    """CR-13: PATCH /profile style choices live under `explicit` and must survive the learned-feature job."""
    existing = {"explicit": {"jargon": 3, "depth": "tldr", "metric": "Revenue growth", "tone": "Direct"}}
    c = _card(existing=existing, experience="Beginner", goals=["Income"])
    assert (c["jargon"], c["depth"], c["metric"], c["tone"]) == (3, "tldr", "Revenue growth", "Direct")
    assert c["metrics"] == ["Revenue growth"]
    assert c["explicit"] == existing["explicit"]
