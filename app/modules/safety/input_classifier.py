"""Input classifier: regex baseline + pluggable Llama Guard 4 hook (SPEC 7.4, 11.2; T-SEC-02).

Detects requests we must refuse and redirect: personalised buy/sell advice, guarantees, price predictions and
price targets, plus prompt-injection attempts. Everything else is "ok" (including factual questions that merely
mention buying or selling).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from app.modules.research.injection import HeuristicScanner

Category = Literal["ok", "advice", "guarantee", "prediction", "price_target", "injection", "unsafe"]


@dataclass(frozen=True, slots=True)
class InputVerdict:
    category: Category
    refuse: bool
    matched: str | None = None


class SafetyClassifier(Protocol):
    """Llama Guard 4 style: True when the text is unsafe. Packaging is unverified (SPEC 17.1 item 6)."""

    def is_unsafe(self, text: str) -> bool: ...


def _c(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


_RULES: tuple[tuple[Category, re.Pattern[str]], ...] = (
    ("guarantee", _c(r"\bguarantee[sd]?\b")),
    ("guarantee", _c(r"\b(?:promise|assure) me\b")),
    ("guarantee", _c(r"\b(?:risk[- ]free|can'?t lose|sure (?:thing|bet)|no[- ]risk)\b")),
    ("price_target", _c(r"\bprice[- ]?target\b")),
    ("price_target", _c(r"\btarget price\b")),
    ("price_target", _c(r"\bhow (?:high|much) (?:will|can|could|would) \w+ (?:go|reach|get|be worth)\b")),
    (
        "prediction",
        _c(
            r"\bwill\b[^?.!]{0,40}\b(?:go up|go down|rise|fall|rally|crash|double|moon|beat the market|outperform|be worth)\b"
        ),
    ),
    (
        "prediction",
        _c(r"\b(?:predict|forecast)\b[^?.!]{0,40}\b(?:price|stock|market|shares?|\$?[A-Z]{2,5})\b"),
    ),
    ("prediction", _c(r"\bwhere (?:will|is) [^?.!]{0,30}\b(?:be|going|headed) (?:in|by|next|tomorrow)\b")),
    (
        "prediction",
        _c(r"\b(?:is|are) [^?.!]{0,30}\bgoing to (?:go up|go down|rise|fall|rally|crash|moon|pump)\b"),
    ),
    ("advice", _c(r"\bshould i (?:buy|sell|short|hold|invest|dump|add|trim|put)\b")),
    ("advice", _c(r"\bwhat (?:should|can|do) i (?:buy|sell|invest|do with my)\b")),
    (
        "advice",
        _c(r"\bwhat (?:stock|stocks|shares|etf)s? (?:should|to|do) (?:i |you )?(?:buy|sell|pick|invest)\b"),
    ),
    ("advice", _c(r"\bwhat to (?:buy|sell|invest in)\b")),
    (
        "advice",
        _c(
            r"\bwhich (?:stock|stocks|shares|etf|one)s? (?:should|to|do you|would you) (?:i )?(?:buy|sell|pick|choose|invest)\b"
        ),
    ),
    (
        "advice",
        _c(
            r"\b(?:tell|give|show) me (?:what|which|a|the|some)[^?.!]{0,25}\b(?:to buy|to sell|stock pick|stocks to buy|best stock)\b"
        ),
    ),
    (
        "advice",
        _c(
            r"\b(?:best|top|hottest|safest) (?:stock|stocks|pick|picks|shares|investment)s? (?:to|for|right now|now|today)\b"
        ),
    ),
    (
        "advice",
        _c(r"\bis \$?[A-Za-z]{1,5} a (?:good|great|safe|smart) (?:buy|investment|stock to buy|bet)\b"),
    ),
    ("advice", _c(r"\b(?:buy|sell) (?:now|today|immediately)\b[^?]{0,10}\?")),
    ("advice", _c(r"\bwhat do you recommend\b")),
    (
        "advice",
        _c(r"\b(?:recommend|suggest) (?:me )?(?:a |some |any )?(?:stock|stocks|shares|etf|investment)s?\b"),
    ),
    ("advice", _c(r"\bshould (?:we|someone like me) (?:buy|sell)\b")),
)


class InputClassifier:
    def __init__(self, hook: SafetyClassifier | None = None) -> None:
        self._hook = hook
        self._scanner = HeuristicScanner()

    def classify(self, text: str) -> InputVerdict:
        for cat, rx in _RULES:
            m = rx.search(text)
            if m:
                return InputVerdict(cat, True, m.group(0))
        scan = self._scanner.scan(text)
        if scan.flagged:
            return InputVerdict("injection", True, ",".join(scan.reasons))
        if self._hook is not None and self._hook.is_unsafe(text):
            return InputVerdict("unsafe", True, "llama-guard")
        return InputVerdict("ok", False)


_default = InputClassifier()


def classify_input(text: str) -> InputVerdict:
    return _default.classify(text)
