"""Output guardrail (SPEC 11.4 product guardrails 1-2; T-SEC-02): no guarantees, predictions, price targets,
"you should buy/sell" or urgency/FOMO language in anything shown to a user.

Phrases inside a negation window ("does not guarantee", "I can't give price targets") are educational and
allowed. Used by the brief verifier (rule d), chat, proposals wording, and as a template scanner in tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NEGATORS = re.compile(
    r"\b(?:no|not|never|cannot|can't|cant|won't|don't|doesn't|didn't|isn't|aren't|nor|neither|without|unable to|"
    r"does not|do not|did not|is not|are not|cannot|refuse to|decline to|avoid|rather than|instead of)\b",
    re.IGNORECASE,
)
_WINDOW = 45


@dataclass(frozen=True, slots=True)
class Pattern:
    category: str
    regex: re.Pattern[str]
    negatable: bool = True


def _p(category: str, pattern: str, negatable: bool = True) -> Pattern:
    return Pattern(category, re.compile(pattern, re.IGNORECASE), negatable)


PATTERNS: tuple[Pattern, ...] = (
    # guarantees
    _p("guarantee", r"\bguarantee[sd]?\b"),
    _p("guarantee", r"\b(?:can'?t|cannot|won'?t|will not) (?:lose|fail|go down)\b", negatable=False),
    _p("guarantee", r"\brisk[- ]?free\b"),
    _p(
        "guarantee",
        r"\b(?:sure thing|sure bet|no[- ]brainer|slam dunk|foolproof|surefire)\b",
        negatable=False,
    ),
    _p("guarantee", r"\bcertain(?:ly)? (?:to|will) (?:rise|go up|increase|grow|double|profit)\b"),
    _p("guarantee", r"\bassured (?:returns?|profits?|gains?)\b"),
    # predictions
    _p(
        "prediction",
        r"\bwill (?:go up|rise|soar|surge|skyrocket|climb|rally|moon|double|triple|crash|plunge|tank|collapse|outperform|beat the market)\b",
    ),
    _p(
        "prediction",
        r"\b(?:is|are) (?:going|set|poised|bound|about|likely|certain|sure|expected) to (?:go up|rise|soar|surge|skyrocket|climb|rally|double|triple|crash|plunge|outperform|beat)\b",
    ),
    _p(
        "prediction",
        r"\b(?:price|stock|shares?) (?:will|should|is going to) (?:be|reach|hit|trade at|go to)\b",
    ),
    _p("prediction", r"\bexpect(?:ed)? to (?:return|gain|earn|deliver) (?:\d|double|triple)"),
    _p(
        "prediction",
        r"\b(?:i|we) (?:predict|forecast|project) (?:that )?(?:the )?(?:stock|price|shares?|it)\b",
    ),
    _p("prediction", r"\b(?:will|to) (?:return|yield|earn|gain) (?:\d|\{\{)"),
    # price targets
    _p("price_target", r"\bprice[- ]target\b"),
    _p("price_target", r"\btarget price\b"),
    _p("price_target", r"\bfair value (?:of|is|at) \$?\d"),
    _p("price_target", r"\b(?:upside|downside) (?:of|to) \$?\d"),
    # advice
    _p(
        "advice",
        r"\byou (?:should|must|ought to|need to|have to) (?:definitely |really |now |immediately )?(?:buy|sell|short|hold|accumulate|dump|purchase|invest in)\b",
    ),
    _p(
        "advice",
        r"\b(?:i|we) (?:recommend|suggest|advise|urge) (?:you )?(?:to )?(?:buy|sell|buying|selling|purchasing|holding|shorting)\b",
    ),
    _p("advice", r"\b(?:strong )?(?:buy|sell) (?:rating|recommendation|signal)\b"),
    _p("advice", r"\b(?:buy|sell) (?:it |this |the stock |the shares |these )?(?:now|today|immediately)\b"),
    _p("advice", r"\b(?:go|get) (?:long|short) (?:on )?\b"),
    _p("advice", r"\b(?:top|best) (?:stock )?pick\b"),
    # urgency / FOMO
    _p("urgency", r"\b(?:act|buy|invest|hurry|move) (?:now|fast|quickly|today)\b"),
    _p("urgency", r"\b(?:don'?t|do not) (?:miss|wait)\b"),
    _p("urgency", r"\b(?:last|final) chance\b"),
    _p("urgency", r"\bbefore it'?s too late\b"),
    _p("urgency", r"\b(?:once[- ]in[- ]a[- ]lifetime|limited[- ]time)\b"),
    _p("urgency", r"\b(?:fomo|to the moon|rocket ship|get in (?:now|early))\b"),
    _p("urgency", r"\blimited (?:window|opportunity)\b"),
)


@dataclass(frozen=True, slots=True)
class Violation:
    category: str
    phrase: str


@dataclass(frozen=True, slots=True)
class GuardResult:
    blocked: bool
    violations: tuple[Violation, ...] = ()

    @property
    def categories(self) -> frozenset[str]:
        return frozenset(v.category for v in self.violations)


def _negated(text: str, start: int) -> bool:
    return bool(NEGATORS.search(text[max(0, start - _WINDOW) : start]))


def check_output(text: str) -> GuardResult:
    found: list[Violation] = []
    for pat in PATTERNS:
        for m in pat.regex.finditer(text):
            if pat.negatable and _negated(text, m.start()):
                continue
            found.append(Violation(pat.category, m.group(0)))
    return GuardResult(bool(found), tuple(found))


def forbidden_phrases(text: str) -> list[str]:
    """Convenience for the brief verifier."""
    return [v.phrase for v in check_output(text).violations]
