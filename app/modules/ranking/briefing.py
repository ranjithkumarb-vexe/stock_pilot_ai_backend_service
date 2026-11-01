"""Daily briefing feed item (FR-11/FR-07). Built by CODE from the user's own portfolio and risk numbers;
no model is involved, so every figure is exact and every sentence is a template (SPEC 0.2: numbers from code)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid5

from app.modules.safety import check_output

BRIEFING_NS = UUID("5b1d2c9e-3e0a-4f6e-9a39-7d7a8c0e41f2")
MATERIALITY = 0.65


def briefing_id(user_id: UUID, day: date) -> UUID:
    return uuid5(BRIEFING_NS, f"{user_id}:briefing:{day.isoformat()}")


def brief_ready_id(user_id: UUID, symbol: str) -> UUID:
    return uuid5(BRIEFING_NS, f"{user_id}:brief:{symbol}")


def money0(x: float) -> str:
    return f"${x:,.0f}"


def pct(x: float, places: int = 2, sign: bool = True) -> str:
    return f"{x:+.{places}f}%" if sign else f"{x:.{places}f}%"


@dataclass(frozen=True, slots=True)
class BriefingInputs:
    first_name: str
    equity: float
    cash: float
    day_change_pct: float
    n_positions: int
    breaches: list[str] = field(default_factory=list)  # e.g. "max position (12.3% vs 10%)"
    rules: dict[str, float | int] = field(default_factory=dict)
    top_item_title: str | None = None
    n_pending: int = 0
    ytd_pct: float | None = None
    spy_pct: float | None = None


def build_briefing(inp: BriefingInputs, day: date, now: datetime) -> dict[str, Any]:
    """The feed `item` of a `briefing`: `{title, body, summary, symbol, generated_at, as_of, greeting, bullets[]}`."""
    bullets: list[dict[str, str]] = []
    if inp.n_positions == 0:
        bullets.append(
            {
                "text": f"Your paper portfolio is {money0(inp.cash)} in cash. Open a brief from your watchlist "
                "or place a first simulated trade.",
                "tone": "accent",
            }
        )
        r = inp.rules
        if r:
            bullets.append(
                {
                    "text": f"Your rules are active: {r.get('pos')}% per position, {r.get('sec')}% per sector, "
                    f"{r.get('trades')} trades a week.",
                    "tone": "neutral",
                }
            )
    else:
        line = f"Portfolio value is {money0(inp.equity)}, {pct(inp.day_change_pct)} today"
        if inp.ytd_pct is not None and inp.spy_pct is not None:
            line += f" and {pct(inp.ytd_pct, 1)} over the tracked period against {pct(inp.spy_pct, 1)} for the S&P 500"
        bullets.append({"text": line + ".", "tone": "neutral"})
        if inp.breaches:
            n = len(inp.breaches)
            bullets.append(
                {
                    "text": f"{n} of your rules {'are' if n > 1 else 'is'} breached: {' and '.join(inp.breaches)}.",
                    "tone": "loss",
                }
            )
        else:
            bullets.append({"text": "All of your rules are within their limits.", "tone": "gain"})
        if inp.top_item_title:
            bullets.append({"text": inp.top_item_title.rstrip(".") + ".", "tone": "info"})
    n = inp.n_pending
    bullets.append(
        {
            "text": f"{n} proposal{'' if n == 1 else 's'} waiting for your decision. "
            "Approving only affects the paper simulator.",
            "tone": "accent",
        }
    )
    for b in bullets:  # defence in depth: the wording rules apply to templates too
        if check_output(b["text"]).blocked:
            b["text"] = "See your portfolio and rules for the current numbers."
    greeting = f"Hello, {inp.first_name}." if inp.first_name else "Hello."
    headline = "Your rules need attention." if inp.breaches else "Your portfolio is within your rules."
    if inp.n_positions == 0:
        headline = "Your workspace is ready."
    return {
        "title": "Daily briefing",
        "greeting": greeting,
        "headline": headline,
        "body": bullets[0]["text"] if len(bullets) == 1 else " ".join(b["text"] for b in bullets[:2]),
        "summary": headline,
        "bullets": bullets,
        "symbol": None,
        "as_of": day.isoformat(),
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "source_label": "Portfolio ledger and risk engine",
    }
