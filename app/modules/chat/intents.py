"""Intent routing in code (not by an LLM): which tools answer this question, and is it answerable at all.

Order matters: help/glossary questions first (they never need data), then the user's own portfolio, then
company questions (metric lookup vs filing text), and finally "unknown" which is answered with an honest
"I cannot find that" (T-RAG-05).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.modules.chat.metrics import metrics_in
from app.modules.chat.symbols import Company, find_companies

Intent = Literal[
    "portfolio_concentration",
    "portfolio_risk",
    "portfolio_overview",
    "filing_metric",
    "filing_text",
    "glossary",
    "app_help",
    "unknown",
]

_MINE = re.compile(
    r"\b(?:my|our|i)\b[^?.!]{0,40}\b(?:portfolio|holdings?|positions?|cash|rules?|account|exposure|stocks?|shares)\b"
    r"|\bmy (?:drawdown|risk|limits?|caps?|concentration|volatility|beta|trades|sector)\b"
    r"|\b(?:portfolio|holdings)\b",
    re.I,
)
_CONCENTRATION = re.compile(
    r"concentrat|diversif|\bhhi\b|herfindahl|effective (?:number|n)\b|top holding|largest (?:position|holding)|"
    r"biggest (?:position|holding)|sector (?:weight|exposure|allocation|breakdown)|overweight|how (?:spread|balanced)",
    re.I,
)
_RISK = re.compile(
    r"\brisk|drawdown|volatil|\bbeta\b|breach|\blimit|\bcap\b|\brules?\b|within my|against my|trades? (?:this|per|a|in)|"
    r"overtrad|how am i doing against",
    re.I,
)
_HELP = re.compile(
    r"\bhow (?:do|can|could|would) i\b[^?]{0,60}\b(?:raise|lower|change|edit|set|update|reset|delete|export|"
    r"turn|enable|disable|add|remove|use)\b"
    r"|\bwhy (?:was|is|am)\b[^?]{0,40}\b(?:proposal|suggest|seeing|alert|ranked|shown)\b"
    r"|\bwhat (?:is|does) (?:the )?(?:why|audit|trace)\b",
    re.I,
)
_GLOSSARY_LEAD = re.compile(
    r"\b(?:what (?:is|are|does|'s)|what's|explain|define|meaning of|difference between|tell me about)\b", re.I
)
_GLOSSARY_TERMS = {
    "pe": re.compile(r"\bp/?e\b|price[- ]to[- ]earnings", re.I),
    "hhi": re.compile(r"\bhhi\b|herfindahl", re.I),
    "10k": re.compile(r"\b10-?k\b", re.I),
    "8k": re.compile(r"\b8-?k\b", re.I),
    "drawdown": re.compile(r"drawdown", re.I),
    "beta": re.compile(r"\bbeta\b", re.I),
    "volatility": re.compile(r"volatil", re.I),
    "diversification": re.compile(r"diversif", re.I),
    "fcf": re.compile(r"free cash ?flow|\bfcf\b", re.I),
    "dividend_yield": re.compile(r"dividend yield", re.I),
    "paper": re.compile(r"paper[- ]trad", re.I),
    "xbrl": re.compile(r"\bxbrl\b", re.I),
    "eps": re.compile(r"\beps\b|earnings per share", re.I),
    "market_cap": re.compile(r"market cap", re.I),
}
_FILING_TOPIC = re.compile(
    r"risk factors?|risks?\b|moat|competit|business|strateg|segment|supplier|supply|regulat|litigation|"
    r"mention|say|discuss|disclos|management|md&a|outlook|customers?|products?|employees|cyber|climate|"
    r"tax|currenc|interest rate|items? ?\d|10-?k|8-?k|filing|annual report",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Route:
    intent: Intent
    companies: tuple[Company, ...] = ()
    metrics: tuple[str, ...] = ()
    topic: str | None = None
    items: tuple[str, ...] = ()  # 10-K items to search (1, 1A, 7, 7A) when the topic is clear
    glossary_key: str | None = None
    notes: list[str] = field(default_factory=list)


def _items_for(message: str) -> tuple[str, ...]:
    if re.search(r"risk|litigation|cyber|regulat|supplier|supply|competit", message, re.I):
        return ("1A",)
    if re.search(r"moat|business|strateg|segment|customers?|products?|employees", message, re.I):
        return ("1",)
    if re.search(r"md&a|management|outlook|liquidity|results of operations", message, re.I):
        return ("7",)
    if re.search(r"interest rate|currenc|market risk", message, re.I):
        return ("7A",)
    return ()


def route(message: str, universe: list[Company]) -> Route:
    msg = message.strip()
    companies = tuple(find_companies(msg, universe))
    mine = bool(_MINE.search(msg))

    if _HELP.search(msg) and not companies:
        return Route("app_help", topic="help")
    if _GLOSSARY_LEAD.search(msg) and not mine and not companies:
        hits = [key for key, rx in _GLOSSARY_TERMS.items() if rx.search(msg)]
        if hits:
            return Route("glossary", glossary_key="8k" if {"10k", "8k"} <= set(hits) else hits[0])
    if mine or (not companies and _CONCENTRATION.search(msg)):
        if _CONCENTRATION.search(msg):
            return Route("portfolio_concentration")
        if _RISK.search(msg):
            return Route("portfolio_risk")
        return Route("portfolio_overview")
    if companies:
        mets = tuple(metrics_in(msg))
        if mets:
            return Route("filing_metric", companies=companies[:3], metrics=mets)
        return Route(
            "filing_text", companies=companies[:2], topic=msg, items=_items_for(msg),
            notes=["topic" if _FILING_TOPIC.search(msg) else "open"],
        )  # fmt: skip
    return Route("unknown", metrics=tuple(metrics_in(msg)))
