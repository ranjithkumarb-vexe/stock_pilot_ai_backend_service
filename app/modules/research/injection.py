"""Prompt-injection scanner for fetched text (SPEC 7.5 step 3, 11.2 LLM01; tests T-SEC-01, T-SEC-05).

Layers
1. `HeuristicScanner` - normalises the text (NFKC, zero-width removal, letter-spacing / leetspeak collapse) and
   matches categorised patterns: instruction override, role hijack, prompt exfiltration, advice/tool
   directives aimed at an assistant, memory poisoning, delimiter / chat-template breakout, exfiltration URLs,
   hidden characters.
2. `ModelScanner` - adapter for a PromptGuard 2 style classifier (`score(text) -> float`, higher = worse). The
   Meta PromptGuard weights are not bundled (SPEC 17.1 item 6 is unverified); register one with
   `register_classifier()` and it is OR-ed with the heuristics.
3. `wrap_untrusted()` - at prompt time every retrieved chunk is wrapped in `<untrusted_document id=...>` with
   `<`/`>` escaped so the text can neither close the wrapper nor open a chat-template tag.

Flagged chunks are quarantined by the ingest pipeline: stored with `quarantined = true`, never embedded and
never returned by retrieval.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

_ZERO_WIDTH = re.compile("[​-‏⁠-⁤﻿‪-‮\U000e0000-\U000e007f­]")
_SPACED_LETTERS = re.compile(r"\b(?:[A-Za-z]\s){3,}[A-Za-z]\b")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


@dataclass(frozen=True, slots=True)
class Rule:
    category: str
    pattern: re.Pattern[str]
    weight: float = 1.0


def _r(category: str, pattern: str, weight: float = 1.0) -> Rule:
    return Rule(category, re.compile(pattern, re.IGNORECASE | re.DOTALL), weight)


_ASSIST = r"(?:assistant|ai|a\.i\.|model|llm|chatbot|language model|copilot|stockpilot|pilot|agent|bot)"
_ADVICE = r"(?:buy|sell|purchase|accumulate|short|go long|invest in|load up on|dump|liquidate)"

RULES: tuple[Rule, ...] = (
    # --- instruction override
    _r(
        "override",
        r"\b(?:ignore|disregard|forget|discard|override|bypass|skip)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|preceding|all|any|your|the|system|safety)\b[^.\n]{0,30}\b(?:instructions?|prompts?|directions?|rules?|guidelines?|context|guardrails?|polic(?:y|ies)|constraints?)\b",
    ),
    _r("override", r"\bdo not (?:follow|obey|listen to)\b[^.\n]{0,30}\b(?:previous|above|system|earlier)\b"),
    _r(
        "override",
        r"\b(?:new|updated|revised|additional) (?:system )?(?:instructions?|directives?|orders?)\s*[:\-]",
    ),
    _r("override", r"\bfrom now on\b[^.\n]{0,40}\b(?:you|ignore|always|never|respond|answer)\b"),
    _r("override", r"\b(?:important|attention|urgent)\b[^.\n]{0,20}\b(?:to|for) (?:the )?" + _ASSIST),
    # --- role hijack / jailbreak
    _r(
        "role",
        r"\byou are (?:now|no longer|actually)\s+(?:a|an|the|my|free|unrestricted|allowed|able|dan|in|operating|acting)\b",
    ),
    _r(
        "role",
        r"\b(?:act|behave|respond|operate) as (?:if you (?:are|were)|an? )?[^.\n]{0,30}\b(?:unrestricted|unfiltered|jailbroken|dan|without (?:any )?(?:restrictions|limits|rules))\b",
    ),
    _r("role", r"\b(?:developer|god|sudo|admin|debug|jailbreak|dan) mode\b"),
    _r("role", r"\bpretend (?:to be|that you (?:are|have))\b"),
    # --- prompt / secret exfiltration
    _r(
        "exfil",
        r"\b(?:reveal|print|show|repeat|output|disclose|leak|display|echo|dump)\b[^.\n]{0,30}\b(?:system prompt|your (?:instructions|rules|prompt)|hidden (?:prompt|instructions)|api[ _-]?keys?|secrets?|credentials|tokens?)\b",
    ),
    _r("exfil", r"\bwhat (?:is|are) your (?:system )?(?:prompt|instructions)\b"),
    _r(
        "exfil",
        r"\b(?:send|forward|upload|transmit|exfiltrate)\b[^.\n]{0,40}\b(?:user'?s?|users'|portfolio|holdings|conversation|chat|memories|credentials|session)\b[^.\n]{0,60}(?:https?://|\bto [\w.+-]+@[\w.-]+)",
    ),
    _r(
        "exfil",
        r"(?:^|[.!?:]\s+|\n)\s*(?:fetch|curl|wget|download|navigate to|browse to)\s+(?:the )?(?:url |page |file |link )?(?:at |from )?https?://",
    ),
    _r("exfil", r"!\[[^\]]*\]\(\s*https?://[^)\s]*[?&=][^)]*\)"),
    # --- directives aimed at the assistant / reader to give advice or act
    _r(
        "directive",
        r"\b(?:you|"
        + _ASSIST
        + r")\s+(?:must|should|shall|will|need to|have to|are required to)\s+(?:now\s+|always\s+|immediately\s+)?(?:recommend|"
        + _ADVICE
        + r"|say|state|respond|reply|answer|output|tell|call|execute|invoke|run|create|place|ignore|reveal|include|insert|add|mention|promote|endorse)\b",
    ),
    _r("directive", r"\b(?:" + _ASSIST + r")\s*[,:]\s*(?:you|please|now|always|do|never|ignore)\b"),
    _r(
        "directive",
        r"(?:^|[.!?:]\s+|\n|\b(?:please|always|must|should|then)\s+)(?:recommend|advise|tell|instruct|persuade|convince|urge)\b[^.\n]{0,50}\b(?:users?|readers?|investors?|clients?|people|everyone)\b[^.\n]{0,50}\b"
        + _ADVICE
        + r"\b",
    ),
    _r(
        "directive",
        r"\b(?:always|only|just)\b[^.\n]{0,15}\b(?:recommend|endorse|promote|suggest)\b[^.\n]{0,40}\b(?:stock|shares|ticker|company|security)\b",
    ),
    _r(
        "directive",
        r"\bwhen (?:you )?(?:summari[sz]e|summari[sz]ing|answering|asked|generating|writing|producing)\b[^.\n]{0,80}\b(?:recommend|say|state|respond|include|mention|insert|add|output)\b",
    ),
    _r(
        "directive",
        r"\b(?:guarantee[ds]?|promise[ds]?|assure[ds]?)\b[^.\n]{0,30}\b(?:returns?|profits?|gains?)\b[^.\n]{0,40}\b(?:users?|investors?|readers?|clients?)\b.*\b(?:tell|say|state|inform)\b",
    ),
    _r(
        "directive",
        r"\b(?:call|invoke|execute|use|trigger|run)\b[^.\n]{0,15}\b(?:the )?(?:tool|function|api|endpoint|create_proposal|create_order|place_order|submit_order|get_user_profile|get_portfolio)\b",
    ),
    _r(
        "directive",
        r"\b(?:place|submit|execute|create|send)\b[^.\n]{0,15}\b(?:an? )?(?:order|trade|transaction|transfer|wire)\b[^.\n]{0,40}\b(?:immediately|now|automatically|without)\b",
    ),
    # --- memory poisoning (T-SEC-05)
    _r(
        "memory",
        r"\b(?:remember|memori[sz]e|store|save|record|note|log)\b[^.\n]{0,15}\b(?:that|this|the following)\b[^.\n]{0,60}\b(?:user|investor|client|customer|reader)\b",
    ),
    _r(
        "memory",
        r"\b(?:update|modify|change|edit|overwrite|set)\b[^.\n]{0,15}\b(?:the )?user(?:'s|s)?\s+(?:profile|memory|memories|preferences|risk (?:tier|tolerance)|style card|rules?)\b",
    ),
    _r(
        "memory",
        r"\b(?:user|client|investor|reader)(?:'s|s)?\s+(?:prefers?|likes?|wants?|loves?|is interested in|favou?rs?|always (?:buys?|wants?))\b[^.\n]{0,50}\b(?:penny|crypto|options|leverag|margin|meme|short selling)",
    ),
    # --- template / delimiter breakout
    _r("delimiter", r"</?\s*untrusted[_ -]?document\b", 2.0),
    _r("delimiter", r"<\|\s*(?:im_start|im_end|system|user|assistant|endoftext)\s*\|>", 2.0),
    _r("delimiter", r"\[/?INST\]|<<\s*/?SYS\s*>>", 2.0),
    _r("delimiter", r"</?\s*(?:system|assistant|instructions?|prompt|tool_call|function_call)\s*>", 2.0),
    _r("delimiter", r"(?:^|\n)\s*#{2,}\s*(?:system|instruction|new task|assistant)\b", 1.0),
    _r("delimiter", r"(?:^|\n)\s*(?:system|assistant)\s*:\s*\S", 1.0),
    _r(
        "delimiter",
        r"\b(?:begin|start|end) (?:of )?(?:system|hidden|secret) (?:prompt|instructions?|message)\b",
        1.0,
    ),
    # --- encoded payloads
    _r("encoded", r"\b(?:decode|base64|execute|run|eval)\b[^.\n]{0,40}[A-Za-z0-9+/]{80,}={0,2}", 1.0),
)

FLAG_THRESHOLD = 1.0


@dataclass(frozen=True, slots=True)
class ScanResult:
    flagged: bool
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)


CLEAN = ScanResult(False, 0.0, ())


class Scanner(Protocol):
    def scan(self, text: str) -> ScanResult: ...


def normalise(text: str) -> str:
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))


def deobfuscate(text: str) -> str:
    """Collapse letter-spacing ("i g n o r e") and digit/symbol leetspeak inside words."""

    def unspace(m: re.Match[str]) -> str:
        return re.sub(r"\s", "", m.group(0))

    collapsed = _SPACED_LETTERS.sub(unspace, text)
    return re.sub(
        r"(?<=[A-Za-z])[0-9@$]+(?=[A-Za-z])|(?<=\b)[0-9@$](?=[A-Za-z]{2,}\b)",
        lambda m: m.group(0).translate(_LEET),
        collapsed,
    )


class HeuristicScanner:
    """Pattern based; deterministic; no network. See module docstring."""

    def __init__(self, rules: Sequence[Rule] = RULES, threshold: float = FLAG_THRESHOLD) -> None:
        self._rules = tuple(rules)
        self._threshold = threshold

    def scan(self, text: str) -> ScanResult:
        reasons: list[str] = []
        score = 0.0
        hidden = len(_ZERO_WIDTH.findall(text))
        if hidden >= 3:
            reasons.append("hidden-characters")
            score += 1.0
        base = normalise(text)
        for variant in dict.fromkeys((base, deobfuscate(base))):
            for rule in self._rules:
                if rule.pattern.search(variant):
                    tag = rule.category
                    if tag not in reasons:
                        reasons.append(tag)
                        score += rule.weight
                    else:
                        score += rule.weight * 0.25
        return ScanResult(score >= self._threshold, round(score, 3), tuple(reasons))


class InjectionClassifier(Protocol):
    """A PromptGuard 2 style model: probability-like score in [0, 1], higher = more likely an injection."""

    def score(self, text: str) -> float: ...


class ModelScanner:
    def __init__(self, classifier: InjectionClassifier, threshold: float = 0.5) -> None:
        self._classifier = classifier
        self._threshold = threshold

    def scan(self, text: str) -> ScanResult:
        s = float(self._classifier.score(text))
        return ScanResult(s >= self._threshold, s, ("promptguard",) if s >= self._threshold else ())


class CompositeScanner:
    def __init__(self, scanners: Sequence[Scanner]) -> None:
        self._scanners = tuple(scanners)

    def scan(self, text: str) -> ScanResult:
        flagged = False
        score = 0.0
        reasons: list[str] = []
        for sc in self._scanners:
            r = sc.scan(text)
            flagged = flagged or r.flagged
            score = max(score, r.score)
            reasons.extend(x for x in r.reasons if x not in reasons)
        return ScanResult(flagged, score, tuple(reasons))


_classifier: InjectionClassifier | None = None


def register_classifier(classifier: InjectionClassifier | None) -> None:
    """Install (or remove with None) the PromptGuard 2 hook used by `default_scanner()`."""
    global _classifier
    _classifier = classifier


def default_scanner() -> Scanner:
    heur = HeuristicScanner()
    return CompositeScanner([heur, ModelScanner(_classifier)]) if _classifier is not None else heur


def wrap_untrusted(doc_id: str, text: str) -> str:
    """Delimit retrieved text for prompts; `<`/`>` are escaped so it cannot close or fake a tag."""
    safe = normalise(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_id = re.sub(r"[^A-Za-z0-9_.:\-]", "_", doc_id)
    return f'<untrusted_document id="{safe_id}">\n{safe}\n</untrusted_document>'
