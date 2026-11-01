"""Refusal templates with an educational redirect (SPEC 11.4: "Should I buy X?" => refusal + decision-framework).

Every template is itself checked against the output guardrail in tests. They never name a security to buy or
sell and never predict.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.safety.input_classifier import Category

FRAMEWORK = (
    "A useful way to think about any idea: (1) does it fit the rules you wrote for position size and sector "
    "limits, (2) what do the company's own risk factors say could go wrong, (3) what has to be true for the "
    "valuation to make sense, and (4) does it match your time horizon."
)


@dataclass(frozen=True, slots=True)
class Refusal:
    category: Category
    text: str
    reason: str


_TEMPLATES: dict[str, tuple[str, str]] = {
    "advice": (
        "I can't tell you what to buy or sell. StockPilot is an educational research tool and all trades here "
        "are simulated. " + FRAMEWORK + " I can open a research brief for any ticker on your watchlist or "
        "check your holdings against your own rules.",
        "advice_request",
    ),
    "guarantee": (
        "Nobody can promise a return, and I don't. Investing involves risk of loss and past performance does "
        "not predict future results. "
        + FRAMEWORK
        + " I can show you what a company disclosed about its risks.",
        "guarantee_request",
    ),
    "prediction": (
        "I don't predict prices or market moves. What I can do is show what a company reported, how its "
        "valuation compares with peers using figures computed from filings, and what it lists as risks. "
        + FRAMEWORK,
        "prediction_request",
    ),
    "price_target": (
        "I don't publish price targets. I can show the valuation ratios computed from the filings and how they "
        "compare with peers, so you can form your own view. " + FRAMEWORK,
        "price_target_request",
    ),
    "injection": (
        "I can't act on that request. I only answer questions about companies, filings and your own portfolio "
        "rules, and I ignore instructions embedded in other text.",
        "injection_attempt",
    ),
    "unsafe": (
        "I can't help with that request. I can answer questions about companies, filings and your portfolio rules.",
        "unsafe_request",
    ),
}


def refusal_for(category: Category) -> Refusal:
    text, reason = _TEMPLATES.get(category, _TEMPLATES["unsafe"])
    return Refusal(category, text, reason)


def all_refusals() -> list[Refusal]:
    return [refusal_for(c) for c in _TEMPLATES]  # type: ignore[arg-type]
