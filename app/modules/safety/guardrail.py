"""Output guardrail (SPEC 11.4 product guardrails 1-2; T-SEC-02): no guarantees, predictions, price targets,
"you should buy/sell" or urgency/FOMO language in anything shown to a user.

Phrases inside a negation window ("does not guarantee", "I can't give price targets") are educational and
allowed. Used by the brief verifier (rule d), chat, proposals wording, and as a template scanner in tests.

The text is folded first (`app.core.textnorm`: NFKC, invisible characters, Cyrillic/Greek look-alikes, accents, line
breaks and letter-spacing) so "you \u0455hould buy" or "g u a r a n t e e d" cannot slip past the patterns (SEC-12).
Case is preserved because the imperative+ticker patterns tell a ticker (AAPL) from a word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.textnorm import fold

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
    _p(
        "prediction",
        r"\b(?:(?-i:\$?[A-Z]{2,5})|stock|shares?|price|it)\s+(?:will|is going to|is set to|is poised to|is expected to|should|could)\s+(?:hit|reach|touch|test|break|top|exceed|surpass|climb to|rise to|fall to|drop to|trade (?:at|near|above|below)|be worth)\s+(?:about |around |roughly |over |above |under )?\$?\d",
    ),
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
        r"\b(?:i|we)(?:'d| would| do| strongly| highly| also| really)*\s+(?:recommend|suggest|advise|urge|encourage)\s+(?:that\s+)?(?:you\s+|investors\s+|clients\s+|everyone\s+)?(?:to\s+)?(?:buy\w*|sell\w*|accumulat\w*|hold\w*|short\w*|purchas\w*|add\w*|load\w*|trim\w*|dump\w*|stock up|liquidat\w*|go long|go short|increas\w*|reduc\w*)",
    ),
    _p("advice", r"\b(?:strong )?(?:buy|sell) (?:rating|recommendation|signal)\b"),
    _p("advice", r"\b(?:buy|sell) (?:it |this |the stock |the shares |these )?(?:now|today|immediately)\b"),
    _p("advice", r"\b(?:go|get) (?:long|short) (?:on )?\b"),
    _p("advice", r"\b(?:top|best) (?:stock )?pick\b"),
    # --- paraphrased / imperative advice (SEC-12). Third-person educational prose ("investors often buy ...") is not matched:
    # these need a first/second-person frame, a sentence-initial imperative, or an unambiguous recommendation idiom.
    _p(
        "advice",
        r"\b(?:it'?s|it is|this is|that'?s|that is)\s+(?:an?\s+)?(?:strong|screaming|clear|obvious|solid|easy|great)\s+(?:buy|sell)\b",
    ),
    _p(
        "advice",
        r"(?:^|[.!?;:]\s+|\b(?:i|we|you|please|just|then|now|and|so)(?:'d|'ll| would| will| could| can| should| might| may| must)?\s+(?:\w+\s+){0,2}?)(?:load up|pile into|go all[- ]in|double down|back up the truck|stock up)\s+(?:on|in|into)\b",
    ),
    _p(
        "advice",
        r"(?:^|[.!?;:]\s+|\b(?:please|just|then|now|and|so|go ahead and|you should|you must|you need to|i'?d|we'?d|i would|we would)\s+)(?:sell|dump|liquidate|exit|dispose of|unload|ditch)\s+(?:everything|all\s+(?:of\s+)?(?:your|the|these|those|it)|your\s+(?:entire\s+|whole\s+)?(?:holdings?|positions?|portfolio|shares|stocks?))\b",
    ),
    _p(
        "advice",
        r"\b(?:is|are|'s)\s+(?:a|an|the)\s+(?:very\s+|really\s+|pretty\s+)?(?:safe|sure|solid|can'?t[- ]miss|no[- ]lose)\s+(?:bet|buy|play|winner)\b",
    ),
    _p(
        "advice",
        r"\b(?:your|the|my)\s+(?:best|smartest|wisest|safest|right|obvious|only|top|better)\s+(?:move|play|option|choice|step|bet|strategy|course of action)\s+(?:is|would be|here is|now is|might be|may be)\s+(?:to\s+)?(?:add|buy|sell|trim|hold|accumulate|purchase|load|dump|exit|short|go|double|increase|reduce)\b",
    ),
    _p(
        "advice",
        r"\b(?:smart|wise|wisest|prudent|sensible|good idea|great idea|best idea|best option|best course)\s+(?:thing\s+)?to\s+(?:do\s+is\s+(?:to\s+)?)?(?:buy|sell|purchase|accumulate|add|load up|dump|short|trim|hold|invest|go long|go short)\b",
    ),
    _p(
        "advice",
        r"(?:^|[.!?;:]\s+|\b(?:please|just|then|now|and|so|go ahead and|you should|you must|you need to|i'?d|we'?d|i would|we would)\s+)(?:buy|purchase|sell|dump|short|accumulate|pick up|grab|scoop up|add to|load up on|trim|liquidate)\s+(?:some\s+|more\s+|a few\s+|all\s+|your\s+|the\s+|my\s+)?(?:shares?\s+(?:of|in)\s+)?(?-i:\$?[A-Z]{2,5})\b",
    ),
    _p(
        "advice",
        r"\b(?:buy|purchase|sell|dump|short|accumulate)\s+(?-i:\$?[A-Z]{1,5})\s+(?:now|today|tomorrow|immediately|right away|asap|this week|before)\b",
    ),
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
    text = fold(text, lower=False)  # SEC-12: look-alikes, invisible characters, line breaks, letter-spacing
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
