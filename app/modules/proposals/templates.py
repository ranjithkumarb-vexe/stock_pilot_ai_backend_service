"""Approved proposal wording (SPEC 11.4). Every proposal text is rendered from a template in this file; nothing
is free-form and nothing comes from a model.

Allowed forms: rule-based ("Your rule max position 15% is exceeded ... Options: reduce, raise the cap, or
dismiss"), educational ("consider reviewing the key risks"), profile-match with risks. Forbidden: "you should
buy/sell", guarantees, "can't lose", price targets, return predictions, urgency / FOMO language.
`assert_clean` is applied to every rendered text at generation time, and a test scans all templates.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

# --- forbidden language ---------------------------------------------------------------------------------
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\byou\s+(should|must|need\s+to|ought\s+to|have\s+to)\s+(buy|sell|trade|exit|dump|add|short)\b",
        r"\b(we|i)\s+(recommend|advise|suggest)\b",
        r"\bguarantee[sd]?\b",
        r"\bcan'?t\s+lose\b",
        r"\bcannot\s+lose\b",
        r"\brisk[- ]free\b",
        r"\bsure\s+(thing|bet|win)\b",
        r"\bprice\s+target\b",
        r"\btarget\s+price\b",
        r"\bwill\s+(go|rise|fall|drop|rally|soar|double|increase)\b",
        r"\bexpected\s+return\b",
        r"\bfomo\b",
        r"\bfear\s+of\s+missing\b",
        r"\b(act|buy|sell)\s+now\b",
        r"\bdon'?t\s+miss\b",
        r"\blast\s+chance\b",
        r"\bhurry\b",
        r"\bbefore\s+it'?s\s+too\s+late\b",
        r"\bto\s+the\s+moon\b",
    )
)


def violations_in(text: str) -> list[str]:
    return [p.pattern for p in FORBIDDEN_PATTERNS if p.search(text)]


def assert_clean(*texts: str) -> None:
    """Two independent guards: this module's SPEC 11.4 patterns and stream B's output guardrail (CR-8)."""
    from app.modules.safety import check_output

    for t in texts:
        bad = violations_in(t)
        verdict = check_output(t)
        if bad or verdict.blocked:
            raise ValueError(
                f"proposal text violates the wording rules ({bad or 'safety guardrail'}): {t[:80]!r}"
            )


# --- templates (str.format fields in braces) ---------------------------------------------------------------
TITLE_POSITION = "Position above your {cap}% cap"
TITLE_SECTOR = "{sector} above your {cap}% sector rule"
TITLE_DROP = "Review {name} after the {drop}% price drop"
TITLE_PACE = "Review your trading pace: {n} trades in 7 days"

BODY_POSITION = (
    "Your rule max position {cap}% is exceeded: {symbol} is {weight}% of your portfolio. "
    "Options: reduce by at least {over} pp, raise the cap, or dismiss. "
    "Selling {qty} shares brings it to {after}%."
)
BODY_SECTOR = (
    "Your rule max sector {cap}% is exceeded: {sector} is {weight}% of your portfolio. "
    "Options: reduce by at least {over} pp, raise the cap, or dismiss. "
    "Selling {qty} shares of {symbol} brings {sector} to {after}%."
)
BODY_DROP = (
    "{name} fell {drop}% versus the prior close. You hold {qty} shares ({weight}% of your portfolio). "
    "Consider reviewing the key risks and the latest filings in the linked sources."
)
BODY_PACE = (
    "Your rule allows {limit} trades per week. In a study of 66,465 households, those who traded most earned "
    "11.4% a year against 17.9% for the market. Consider reviewing whether each trade matched a written reason."
)

RATIONALE_POSITION = "Your rule: maximum position {cap}%. {symbol} is {weight}% of your portfolio. Selling {qty} shares brings it to {after}%."
RATIONALE_SECTOR = (
    "Your rule: maximum sector {cap}%. {sector} is {weight}% of your portfolio. "
    "Selling {qty} shares of {symbol} brings it to {after}%."
)
RATIONALE_SECTOR_PICK = "{symbol} has the smallest unrealised gain among the {sector} holdings that are inside their own position cap."
RATIONALE_DROP = "A one-day move of {drop}% or more on a holding is flagged for review. This is a prompt to read, not a signal to trade."
RATIONALE_PACE = "{n} of your {limit} weekly trades are used. The weekly limit is a rule you set."
RATIONALE_ENGINE = "The numbers come from the deterministic risk engine, not from a model."
RATIONALE_NEVER_LOOSENED = "Your rules are never loosened automatically."
RATIONALE_NO_TRADE = "Acknowledging this review places no order."

SOURCE_RISK_POS = "Risk engine · rule R-POS"
SOURCE_RISK_SEC = "Risk engine · rule R-SEC"
SOURCE_PRICE = "Price data: last price vs prior close"
SOURCE_PACE = "Behaviour detector · overtrading"
SOURCE_STUDY = "Barber and Odean (2000), Trading Is Hazardous to Your Wealth"

WHY_PINNED = "Pinned: rule breaches always show and bypass learned ranking."
WHY_REVIEW = "Shown because it matches a review trigger on your own holdings or activity."
FEATURE_BREACH = "Rule breach: {detail}"
FEATURE_MOVE = "Large one-day move on a holding you own: {detail}"
FEATURE_PACE = "Trading pace near your weekly limit: {detail}"

REJECT_REASONS = ("too_risky", "disagree", "timing", "already_know")


def all_template_strings() -> list[str]:
    """Every static wording string in this module (scanned by the forbidden-phrase test)."""
    out: list[str] = []
    for name, value in globals().items():
        if name.isupper() and isinstance(value, str) and name not in {"REJECT_REASONS"}:
            out.append(value)
    return out


def pct1(x: Decimal) -> str:
    return format(x.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f")


def fmt_cap(x: Decimal) -> str:
    return format(x.normalize(), "f")
