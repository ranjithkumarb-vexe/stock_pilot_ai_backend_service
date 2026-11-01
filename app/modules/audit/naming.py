"""Presentation of stored traces for the Audit screen (no user free text is ever stored in a trace)."""

from __future__ import annotations

from typing import Any

_INTENT_TEXT = {
    "portfolio_concentration": "portfolio concentration question",
    "portfolio_risk": "portfolio risk and rules question",
    "portfolio_overview": "portfolio overview question",
    "filing_metric": "reported-figures question",
    "filing_text": "filing-disclosure question",
    "glossary": "definition question",
    "app_help": "how-to question",
    "unknown": "question outside the sources",
    "pending": "question",
}
_REFUSED_TEXT = {
    "advice": "advice request",
    "guarantee": "return-guarantee request",
    "prediction": "price-prediction request",
    "price_target": "price-target request",
    "injection": "prompt-injection attempt",
    "unsafe": "unsafe request",
}


def kind_of(name: str) -> str:
    head = name.split(":", 1)[0]
    return head if head in ("brief", "chat", "ingest") else "other"


def display_name(name: str) -> str:
    parts = name.split(":")
    head = parts[0]
    if head == "brief" and len(parts) > 1:
        return f"Brief: {parts[1]}"
    if head == "chat":
        if len(parts) > 2 and parts[1] == "refused":
            return f"Chat: {_REFUSED_TEXT.get(parts[2], 'request')} (refused)"
        return f"Chat: {_INTENT_TEXT.get(parts[1] if len(parts) > 1 else 'pending', 'question')}"
    if head == "ingest" and len(parts) > 2 and parts[1] == "injection":
        return f"Injected {parts[3] if len(parts) > 3 else 'filing'} chunk ({parts[2]})"
    return name


def mode_label(mode: str | None, model: str | None, outcome: str, name: str) -> str:
    if not model:
        if kind_of(name) == "ingest":
            return "Blocked before LLM"
        if name.startswith("chat:refused"):
            return "Refused by input policy"
        return "Blocked before LLM" if outcome == "blocked" else "No model call"
    if mode == "contributor":
        return "Contributor · public data"
    return (
        "Standard · contains user data"
        if kind_of(name) == "chat"
        else "Standard · user style card + public data"
    )


def step_detail(detail: dict[str, Any] | None) -> str:
    d = dict(detail or {})
    summary = d.pop("summary", None)
    extra = ", ".join(f"{k}: {v}" for k, v in d.items() if v not in (None, "", [], {}))
    if summary and extra:
        return f"{summary} ({extra})"
    return str(summary) if summary else extra


def step_outcome(outcome: str) -> str:
    return (
        outcome
        if outcome in ("ok", "blocked", "error")
        else ("ok" if outcome in ("pass", "success") else "error")
    )
