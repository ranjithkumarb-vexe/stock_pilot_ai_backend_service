"""Questionnaire, tier labels and scoring - identical to the design (`QUESTIONS`, `TIERS`, tier = clamp(round(mean)))."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.modules.risk import Rules, default_rules_for_tier

N_QUESTIONS = 6

QUESTIONS: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    ("Your portfolio falls 20% in one month. What do you do?",
     (("Sell everything", 1), ("Sell some to reduce risk", 2), ("Hold and wait", 4), ("Buy more at lower prices", 5))),
    ("Which one-year outcome range would you accept?",
     (("+4% best case, -2% worst case", 1), ("+8% / -8%", 2), ("+14% / -16%", 3), ("+25% / -30%", 5))),
    ("How stable is your income?",
     (("Variable; I may need this money soon", 1), ("Stable", 3), ("Very stable with regular surplus", 4))),
    ("A stock you own rises 30% in two weeks.",
     (("Sell all to lock in the gain", 1), ("Sell part", 2), ("Hold", 3), ("Add to the position", 5))),
    ("How would you feel about one stock being 20% of your portfolio?",
     (("Uncomfortable", 1), ("Acceptable for a high-conviction idea", 3), ("Comfortable", 5))),
    ("Have you been invested through a market decline?",
     (("No", 2), ("Yes, and I held", 3), ("Yes, and I bought more", 5))),
)  # fmt: skip

TIER_LABELS: tuple[str, ...] = ("", "Very conservative", "Conservative", "Balanced", "Growth", "Aggressive")
HORIZONS: tuple[str, ...] = ("Under 1 year", "1-3 years", "3-10 years", "10+ years")
EXPERIENCE_LEVELS: tuple[str, ...] = ("Beginner", "Intermediate", "Advanced")
GOALS: tuple[str, ...] = ("Growth", "Income", "Preservation")
ESG_EXCLUSIONS: tuple[str, ...] = ("Tobacco", "Weapons", "Fossil fuels", "Gambling")
SUPPORTED_REGION = "US"
SUPPORTED_CURRENCY = "USD"
BANDIT_ARMS: tuple[str, ...] = ("breach", "proposal", "filing", "move", "earnings", "mirror", "brief")
FACTOR_DIMS = {"Dividend", "Quality", "Value", "Growth", "Momentum"}

_TIER_TEXT = {
    1: "Your answers point to protecting capital and keeping swings small.",
    2: "Your answers point to steady results with limited swings.",
    3: "Your answers point to a middle path between steadiness and growth.",
    4: "Your answers point to accepting larger swings in pursuit of growth.",
    5: "Your answers point to accepting large swings in pursuit of growth.",
}


@dataclass(frozen=True, slots=True)
class TierResult:
    tier: int
    label: str
    average: Decimal


def valid_scores(index: int) -> set[int]:
    return {score for _, score in QUESTIONS[index][1]}


def score_answers(answers: list[int]) -> TierResult:
    """mean of the six scores, rounded half UP (JS `Math.round`), clamped to 1..5."""
    if len(answers) != N_QUESTIONS:
        raise ValueError(f"expected {N_QUESTIONS} answers")
    avg = Decimal(sum(answers)) / Decimal(len(answers))
    tier = int((avg + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR))
    tier = max(1, min(5, tier))
    return TierResult(tier, TIER_LABELS[tier], avg)


def explanation_for(result: TierResult, rules: Rules) -> str:
    """Plain, non-advisory text: how the number was derived and what it sets (SPEC 11.4 wording rules)."""
    avg = result.average.quantize(Decimal("0.1"))
    r = rules
    return (
        f"Your six answers average {avg}, which rounds to tier {result.tier} ({result.label}). "
        f"{_TIER_TEXT[result.tier]} "
        f"Starting rules for this tier: maximum position {r.pos:g}%, maximum sector {r.sec:g}%, "
        f"drawdown alert {r.dd:g}%, {r.trades} trades per week and a {r.cash:g}% cash buffer. "
        "You can edit any rule; StockPilot never loosens a rule on its own."
    )


def tier_rules(tier: int) -> Rules:
    return default_rules_for_tier(tier)


def interest_dim(name: str) -> str:
    return f"factor:{name.lower()}" if name in FACTOR_DIMS else f"sector:{name}"


def initial_interests(likes: list[str], goals: list[str]) -> list[tuple[str, float]]:
    """Design onboarding: liked sectors 0.6, Income->Dividend 0.6, Growth->Growth 0.6, Preservation->Quality 0.5."""
    out: list[tuple[str, float]] = [(like, 0.6) for like in likes]
    if "Income" in goals:
        out.append(("Dividend", 0.6))
    if "Growth" in goals:
        out.append(("Growth", 0.6))
    if "Preservation" in goals:
        out.append(("Quality", 0.5))
    return out


def default_style(experience: str, goals: list[str]) -> dict[str, object]:
    jargon = {"Beginner": 1, "Intermediate": 2, "Advanced": 3}[experience]
    metric = "Dividend yield" if "Income" in goals else "Revenue growth" if "Growth" in goals else "P/E"
    order = (
        ["bal", "risk", "moat", "val", "trend", "cat", "bb"]
        if "Income" in goals
        else ["trend", "cat", "val", "risk", "moat", "bal", "bb"]
    )
    return {
        "depth": "standard",
        "jargon": jargon,
        "tone": "Plain language",
        "metric": metric,
        "language": "English",
        "length": "medium",
        "quant_vs_narrative": 0.5,
        "section_order": order,
    }
