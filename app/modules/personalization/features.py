"""Behavioural features computed from a user's own events (SPEC 10.3 B/C/E/F). Pure functions, no I/O.

Half-life is 14 days for implicit features. Everything is recomputed from scratch from the event log (idempotent:
running it twice gives the same result), never accumulated incrementally.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

HALF_LIFE_DAYS = 14.0
ENGAGED_MS = 8_000  # dwell > 8 s = engaged
SKIMMED_MS = 2_000  # dwell < 2 s = skimmed
PIN_CLICKS = 3  # same metric >= 3 times ...
PIN_WINDOW = timedelta(days=7)  # ... in 7 days => pinned
VIEW_WEIGHT = 0.10  # interest += 0.1 * decay per ticker view (SPEC 10.3 B)
BRIEF_WEIGHT = 0.05
APPROVE_WEIGHT = 0.05
DEFAULT_SECTION_ORDER = ("trend", "bal", "val", "moat", "risk", "cat", "bb")
SECTION_TITLES = {
    "trend": "Trend & growth",
    "bal": "Balance sheet & dividend",
    "val": "Valuation",
    "moat": "Moat",
    "risk": "Risks",
    "cat": "Catalysts",
    "bb": "Bull & bear",
}
TOPIC_TO_FACTOR = {
    "valuation": "factor:value",
    "value": "factor:value",
    "dividends": "factor:dividend",
    "dividend": "factor:dividend",
    "growth": "factor:growth",
    "quality": "factor:quality",
    "momentum": "factor:momentum",
}
EXPERIENCE_JARGON = {"beginner": 1, "intermediate": 2, "advanced": 3, "expert": 3}
GOAL_METRIC = {"income": "dividend_yield", "growth": "revenue_growth", "preservation": "debt_to_equity"}


@dataclass(frozen=True, slots=True)
class EventRow:
    event_id: str
    ts: datetime
    type: str
    entity: Mapping[str, Any]
    props: Mapping[str, Any]


def decay(age_days: float, half_life: float = HALF_LIFE_DAYS) -> float:
    return float(0.5 ** (max(0.0, age_days) / half_life))


def _w(ev: EventRow, now: datetime) -> float:
    return decay((now - ev.ts).total_seconds() / 86400.0)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------------------- interest vector
def interest_contributions(
    events: Iterable[EventRow], sector_of: Mapping[str, str], now: datetime
) -> dict[str, float]:
    """Decayed evidence per interest dim (`sector:<Sector>` / `factor:<name>`), before adding the baseline."""
    out: dict[str, float] = defaultdict(float)
    for ev in events:
        w = _w(ev, now)
        ticker = ev.entity.get("ticker")
        sector = sector_of.get(ticker) if isinstance(ticker, str) else None
        if ev.type == "ticker_viewed" and sector:
            out[f"sector:{sector}"] += VIEW_WEIGHT * w
        elif ev.type == "brief_opened" and sector:
            out[f"sector:{sector}"] += BRIEF_WEIGHT * w
        elif ev.type == "proposal_approved" and sector:
            out[f"sector:{sector}"] += APPROVE_WEIGHT * w
        elif ev.type == "chat_query":
            for topic in ev.props.get("topics") or []:
                dim = TOPIC_TO_FACTOR.get(topic)
                if dim:
                    out[dim] += BRIEF_WEIGHT * w
    return dict(out)


def merge_interest(baseline: Mapping[str, float], contrib: Mapping[str, float]) -> dict[str, float]:
    dims = set(baseline) | set(contrib)
    return {d: round(_clamp(baseline.get(d, 0.0) + contrib.get(d, 0.0)), 6) for d in sorted(dims)}


def interest_drift(
    events: Sequence[EventRow],
    sector_of: Mapping[str, str],
    now: datetime,
    *,
    recent_days: int = 7,
    prior_days: int = 21,
) -> dict[str, Any]:
    """KL(recent || prior) of the normalised interest evidence (SPEC 10.8: > 0.3 => 'has your focus changed?')."""
    cut = now - timedelta(days=recent_days)
    start = cut - timedelta(days=prior_days)
    recent = interest_contributions([e for e in events if e.ts >= cut], sector_of, now)
    prior = interest_contributions([e for e in events if start <= e.ts < cut], sector_of, now)
    if sum(recent.values()) < 0.2 or sum(prior.values()) < 0.2:
        return {"kl": 0.0, "flag": False, "enough_data": False}
    dims = sorted(set(recent) | set(prior))
    eps = 0.01
    pr = [recent.get(d, 0.0) + eps for d in dims]
    pp = [prior.get(d, 0.0) + eps for d in dims]
    sr, sp = sum(pr), sum(pp)
    kl = sum((a / sr) * math.log((a / sr) / (b / sp)) for a, b in zip(pr, pp, strict=True))
    return {"kl": round(kl, 4), "flag": kl > 0.3, "enough_data": True}


# ------------------------------------------------------------------------------- topics / sections
def topic_histogram(events: Iterable[EventRow], now: datetime) -> dict[str, float]:
    raw: dict[str, float] = defaultdict(float)
    for ev in events:
        if ev.type == "chat_query":
            for topic in ev.props.get("topics") or []:
                raw[str(topic)] += _w(ev, now)
    total = sum(raw.values())
    return {t: round(v / total, 4) for t, v in sorted(raw.items(), key=lambda kv: -kv[1])} if total else {}


def section_beta(events: Iterable[EventRow], now: datetime) -> dict[str, dict[str, float]]:
    """Beta(alpha, beta) per section type: engaged (dwell > 8 s, or an expansion) vs skimmed (dwell < 2 s)."""
    a: dict[str, float] = defaultdict(float)
    b: dict[str, float] = defaultdict(float)
    for ev in events:
        sec = ev.entity.get("section")
        if not isinstance(sec, str):
            continue
        w = _w(ev, now)
        dwell = ev.props.get("dwell_ms")
        if ev.type == "section_dwell" and isinstance(dwell, int | float):
            if dwell > ENGAGED_MS:
                a[sec] += w
            elif dwell < SKIMMED_MS:
                b[sec] += w
        elif ev.type == "section_expanded":
            if isinstance(dwell, int | float) and dwell < SKIMMED_MS:
                b[sec] += w
            else:
                a[sec] += w
    out: dict[str, dict[str, float]] = {}
    for sec in sorted(set(a) | set(b)):
        al, be = 1.0 + a[sec], 1.0 + b[sec]  # Beta(1,1) prior
        out[sec] = {"alpha": round(al, 4), "beta": round(be, 4), "mean": round(al / (al + be), 4)}
    return out


def section_order(
    betas: Mapping[str, Mapping[str, float]],
    fallback: Sequence[str] = DEFAULT_SECTION_ORDER,
    min_evidence: float = 3.0,
) -> list[str]:
    """Order by posterior mean when there is enough evidence, else keep `fallback` (cold start)."""
    evidence = sum(v["alpha"] + v["beta"] - 2.0 for v in betas.values())
    base = [s for s in fallback if s in SECTION_TITLES] or list(DEFAULT_SECTION_ORDER)
    for s in DEFAULT_SECTION_ORDER:
        if s not in base:
            base.append(s)
    if evidence < min_evidence:
        return base
    return sorted(base, key=lambda s: (-betas.get(s, {}).get("mean", 0.5), base.index(s)))


def pinned_metrics(events: Iterable[EventRow], now: datetime) -> list[str]:
    counts: Counter[str] = Counter()
    for ev in events:
        metric = ev.entity.get("metric")
        if ev.type == "metric_clicked" and isinstance(metric, str) and now - ev.ts <= PIN_WINDOW:
            counts[metric] += 1
    return [m for m, n in counts.most_common() if n >= PIN_CLICKS][:3]


# ------------------------------------------------------------------------------- style card
def _count(events: Iterable[EventRow], now: datetime, types: set[str], within_days: int | None = 30) -> float:
    return sum(
        _w(e, now) if within_days is None else 1.0
        for e in events
        if e.type in types and (within_days is None or (now - e.ts).days <= within_days)
    )


def derive_style_card(
    *,
    existing: Mapping[str, Any] | None,
    experience: str | None,
    goals: Sequence[str],
    events: Sequence[EventRow],
    betas: Mapping[str, Mapping[str, float]],
    pinned: Sequence[str],
    now: datetime,
    default_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """SPEC 10.3 E. `tone` and `language` are explicit settings and are never overwritten."""
    card: dict[str, Any] = dict(existing or {})
    briefs = _count(events, now, {"brief_opened"})
    expanded = _count(events, now, {"section_expanded"})
    glossary = _count(events, now, {"glossary_clicked"})
    clicks = _count(events, now, {"metric_clicked"})

    depth = card.get("depth") if card.get("depth") in ("tldr", "standard", "deep") else "standard"
    if briefs >= 5:
        per_brief = expanded / briefs
        depth = "deep" if per_brief >= 3 else "tldr" if per_brief <= 1 else "standard"

    base_jargon = EXPERIENCE_JARGON.get((experience or "").lower())
    jargon = base_jargon or (card["jargon"] if card.get("jargon") in (1, 2, 3) else 2)
    if briefs >= 5:
        if glossary / briefs >= 1:
            jargon = max(1, jargon - 1)  # keeps looking terms up: explain more plainly
        elif glossary == 0 and base_jargon is not None and expanded / briefs >= 2:
            jargon = min(3, jargon + 1)

    dwells = [
        float(e.props["dwell_ms"])
        for e in events
        if e.type == "section_dwell" and isinstance(e.props.get("dwell_ms"), int | float)
    ]
    length = card.get("length") if card.get("length") in ("short", "medium", "long") else "medium"
    if len(dwells) >= 10:
        med = statistics.median(dwells)
        length = "short" if med < 3_000 else "long" if med > 12_000 else "medium"

    metrics: list[str]
    if pinned:
        metrics = list(pinned)
    elif isinstance(card.get("metrics"), list) and card["metrics"]:
        metrics = [str(m) for m in card["metrics"]][:3]
    elif isinstance(card.get("metric"), str) and card["metric"]:
        metrics = [card["metric"]]
    else:
        metrics = [next((GOAL_METRIC[g.lower()] for g in goals if g.lower() in GOAL_METRIC), "pe")]

    narrative = expanded + glossary
    qvn = round((clicks + 1.5) / (clicks + narrative + 3.0), 3)

    raw_order = card.get("order") if isinstance(card.get("order"), list) else card.get("section_order")
    fallback_order: list[str] = (
        [str(x) for x in raw_order]
        if isinstance(raw_order, list)
        else list(default_order or DEFAULT_SECTION_ORDER)
    )
    ordered = section_order(betas, fallback_order)
    card.update(
        {
            "depth": depth,
            "jargon": jargon,
            "length": length,
            "tone": card.get("tone") or "neutral",
            "metrics": metrics,
            "metric": metrics[0],
            "quant_vs_narrative": qvn,
            "language": card.get("language") or "English",
            "section_order": ordered,
            "order": ordered,  # the key stream B's brief generator reads (CR-5); section_order kept for the UI/seed
        }
    )
    # CR-13: an explicit user choice (PATCH /profile style fields, stored under `explicit`) always wins over
    # what the job derived, so the stored top-level fields that the brief generator reads keep the choice.
    explicit = card.get("explicit")
    if isinstance(explicit, dict):
        card.update(explicit)
        if isinstance(explicit.get("metric"), str) and explicit["metric"]:
            card["metrics"] = [explicit["metric"]]
    return card
