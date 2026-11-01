"""Alert detectors (SPEC 7.7). Pure: they turn already-loaded inputs into `AlertDraft`s; the service decides
what is new (dedupe) and inserts. Breach verdicts come from the real risk engine (`risk.current_violations`)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.modules.alerts import materiality as mat
from app.modules.risk import PortfolioMetrics, Position, Rules, Violation, current_violations

HHI_ALERT = 0.25  # SPEC 10.3 G: HHI > 0.25 is concentrated
EARNINGS_WINDOW_DAYS = 3
MIN_RETURNS = 30
VOL_WINDOW = 60
BREACH_SOURCE = {
    "pos": "Risk engine · rule R-POS",
    "sec": "Risk engine · rule R-SEC",
    "dd": "Risk engine · rule R-DD",
    "trades": "Risk engine · rule R-TRADES",
    "cash": "Risk engine · rule R-CASH",
}
ITEM_DESC = {
    "1.01": "material agreement",
    "1.02": "termination of an agreement",
    "1.03": "bankruptcy",
    "2.01": "acquisition or disposition",
    "2.02": "quarterly results",
    "2.04": "obligation accelerated",
    "2.05": "exit or disposal costs",
    "2.06": "material impairment",
    "3.01": "listing notice",
    "3.03": "change to shareholder rights",
    "4.01": "change of auditor",
    "4.02": "financial restatement notice",
    "5.01": "change in control",
    "5.02": "officer change",
    "5.03": "bylaw amendment",
    "5.07": "shareholder vote",
    "7.01": "Regulation FD disclosure",
    "8.01": "other event",
    "9.01": "financial statements and exhibits",
}
SOURCE_NAMES = {"sec_edgar": "SEC EDGAR", "raw_documents": "ingested filings", "finnhub_news": "Finnhub news"}
_SUBJECT_RE = re.compile(r"^(?P<subject>.+?) is [\d.]+% of your portfolio")


@dataclass(frozen=True, slots=True)
class AlertDraft:
    type: str
    symbol: str | None
    critical: bool
    materiality: float
    title: str
    body: str
    source_label: str
    dedupe_key: str
    suppress_key: str | None = None  # 24 h suppression group (breach_key / "move:SYM" ...)
    payload: dict[str, Any] = field(default_factory=dict)
    event_at: datetime | None = None  # when the underlying event happened (latency measurement)

    def to_payload(self) -> dict[str, Any]:
        out = {
            "title": self.title,
            "body": self.body,
            "source_label": self.source_label,
            **self.payload,
        }
        if self.suppress_key:
            out["suppress_key"] = self.suppress_key
        return out


def _day(now: datetime) -> str:
    return now.date().isoformat()


def _pct(x: float, places: int = 1) -> str:
    return f"{x:.{places}f}"


def _cap(x: Any) -> str:
    d = float(x)
    return f"{int(d)}" if d == int(d) else f"{d:g}"


def violation_subject(v: Violation) -> str | None:
    """Symbol / sector a pos|sec violation talks about (the engine's message starts with it)."""
    m = _SUBJECT_RE.match(v.message)
    return m.group("subject") if m else None


# ------------------------------------------------------------------ rule breaches (risk engine)
def detect_breaches(
    metrics: PortfolioMetrics, rules: Rules, positions: Sequence[Position], now: datetime
) -> list[AlertDraft]:
    """One critical alert per CURRENT violation reported by `risk.current_violations`."""
    violations = current_violations(metrics, rules)
    pos_breached = {violation_subject(v) for v in violations if v.rule == "pos"}
    out: list[AlertDraft] = []
    for v in violations:
        subject = violation_subject(v) if v.rule in ("pos", "sec") else None
        limit = float(v.limit) if v.limit is not None else 0.0
        value = float(v.value) if v.value is not None else limit
        overage = min(20.0, (value - limit) * 5.0) if v.rule == "trades" else value - limit
        symbol: str | None = None
        if v.rule == "pos" and subject:
            title, body = (
                f"Position cap breached: {subject}",
                f"{subject} is above your {_cap(limit)}% maximum position rule.",
            )
            symbol = subject
        elif v.rule == "sec" and subject:
            title, body = (
                f"Sector rule breached: {subject}",
                f"{subject} exceeds your {_cap(limit)}% sector cap.",
            )
            members = [p for p in positions if (p.sector or "Unclassified") == subject]
            pool = [p for p in members if p.symbol not in pos_breached] or members
            symbol = max(pool, key=lambda p: (p.value, p.symbol)).symbol if pool else None
        elif v.rule == "dd":
            title, body = "Drawdown limit reached", v.message
        elif v.rule == "trades":
            title, body = "Weekly trade limit exceeded", v.message
        elif v.rule == "cash":
            title, body = "Cash buffer below your minimum", v.message
        else:
            title, body = v.label, v.message
        key = f"{v.rule}:{subject or ''}"
        out.append(
            AlertDraft(
                type="breach",
                symbol=symbol,
                critical=True,
                materiality=mat.breach_materiality(overage),
                title=title,
                body=body,
                source_label=BREACH_SOURCE.get(v.rule, "Risk engine"),
                dedupe_key=f"breach:{key}:{_day(now)}",
                suppress_key=f"breach:{key}",
                payload={
                    "rule": v.rule,
                    "subject": subject,
                    "breach_key": key,
                    "value": round(value, 2),
                    "limit": round(limit, 2),
                    "engine": "risk.current_violations",
                },
                event_at=now,
            )
        )
    return out


def detect_concentration(metrics: PortfolioMetrics, now: datetime) -> list[AlertDraft]:
    if metrics.hhi is None or metrics.effective_n is None or metrics.hhi <= HHI_ALERT:
        return []
    over = metrics.hhi - HHI_ALERT
    m = mat.materiality(mat.TYPE_WEIGHTS["concentration"], mat.clamp01(over / 0.25), 1.0, 1.0)
    return [
        AlertDraft(
            type="concentration",
            symbol=None,
            critical=False,
            materiality=m,
            title="Portfolio concentration is high",
            body=(
                f"Your holdings behave like {metrics.effective_n:.1f} equal-sized positions "
                f"(HHI {metrics.hhi:.2f}, above the {HHI_ALERT:.2f} concentration mark)."
            ),
            source_label="Risk engine · concentration (HHI)",
            dedupe_key=f"concentration:hhi:{_day(now)}",
            suppress_key="concentration:hhi",
            payload={"hhi": round(metrics.hhi, 4), "effective_n": round(metrics.effective_n, 2)},
            event_at=now,
        )
    ]


# ------------------------------------------------------------------ 8-K filings
@dataclass(frozen=True, slots=True)
class FilingSignal:
    """One 8-K as seen by one source. Several sources may report the same accession (T-ALERT-01)."""

    symbol: str
    accession_no: str
    items: tuple[str, ...]
    published_at: datetime
    source: str
    url: str | None = None


def merge_signals(signals: Sequence[FilingSignal]) -> list[tuple[FilingSignal, list[str]]]:
    """Collapse the same `(symbol, accession_no)` seen by several sources into one signal + its source list."""
    grouped: dict[tuple[str, str], list[FilingSignal]] = {}
    for s in signals:
        grouped.setdefault((s.symbol, s.accession_no), []).append(s)
    out: list[tuple[FilingSignal, list[str]]] = []
    for _, group in sorted(grouped.items()):
        first = min(group, key=lambda s: s.published_at)
        items = tuple(dict.fromkeys(i for s in group for i in s.items))
        url = next((s.url for s in group if s.url), None)
        merged = FilingSignal(first.symbol, first.accession_no, items, first.published_at, first.source, url)
        out.append((merged, sorted({s.source for s in group})))
    return out


def detect_filings(
    signals: Sequence[FilingSignal],
    *,
    names: Mapping[str, str],
    weights_pct: Mapping[str, float],
    watch: set[str],
    now: datetime,
) -> list[AlertDraft]:
    out: list[AlertDraft] = []
    for sig, sources in merge_signals(signals):
        held = sig.symbol in weights_pct
        if not held and sig.symbol not in watch:
            continue
        items = list(sig.items)
        lead = max(items, key=lambda i: mat.ITEM_WEIGHTS.get(i, mat.DEFAULT_ITEM_WEIGHT)) if items else None
        desc = ITEM_DESC.get(lead or "", "current report")
        age_min = max(0.0, (now - sig.published_at).total_seconds() / 60)
        m = mat.materiality(
            mat.filing_type_weight(items),
            mat.filing_magnitude(items),
            mat.exposure(weights_pct.get(sig.symbol), held=held),
            mat.recency(age_min),
        )
        covered = ", ".join(ITEM_DESC.get(i, f"item {i}") for i in items) or "a current report"
        others = [SOURCE_NAMES.get(s, s) for s in sources if s != "sec_edgar"]
        label = (
            f"SEC EDGAR 8-K + {', '.join(others)} ({len(sources)} sources, 1 alert)"
            if len(sources) > 1
            else "SEC EDGAR 8-K"
        )
        name = names.get(sig.symbol, sig.symbol)
        out.append(
            AlertDraft(
                type="filing",
                symbol=sig.symbol,
                critical=False,
                materiality=m,
                title=f"{sig.symbol} filed 8-K" + (f" (Item {lead}): {desc}" if lead else ""),
                body=f"{name} filed a current report covering: {covered}. Open the filing for the full text.",
                source_label=label,
                dedupe_key=f"filing:{sig.symbol}:{sig.accession_no}",
                suppress_key=None,
                payload={
                    "accession_no": sig.accession_no,
                    "items": items,
                    "sources": sources,
                    "url": sig.url,
                    "published_at": sig.published_at.isoformat(),
                },
                event_at=sig.published_at,
            )
        )
    return out


# ------------------------------------------------------------------ unusual moves
def daily_sigma(closes: Sequence[float], window: int = VOL_WINDOW) -> float | None:
    """Sample std-dev of the last `window` daily simple returns; None when history is too short / degenerate."""
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-window:]
    if len(rets) < MIN_RETURNS:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return sd if sd > 1e-9 else None


def zscore(ret: float, sigma: float) -> float:
    return ret / sigma


def detect_move(
    *,
    symbol: str,
    price: float,
    prev_close: float | None,
    sigma: float | None,
    k: float,
    quote_allows: bool,
    weight_pct: float | None,
    held: bool,
    now: datetime,
    source: str,
    freshness: str,
    history_note: str | None = None,
) -> AlertDraft | None:
    """|z| > k on the one-day return vs 60-day vol. Suppressed when the quote is stale / market closed
    (`quote_allows` is `marketdata.freshness.allows_move_alerts`, T-DATA-02) or the history is unusable."""
    if not quote_allows or prev_close is None or prev_close <= 0 or sigma is None or sigma <= 0 or price <= 0:
        return None
    ret = price / prev_close - 1.0
    z = zscore(ret, sigma)
    if abs(z) <= k:
        return None
    m = mat.materiality(
        mat.TYPE_WEIGHTS["move"], mat.move_magnitude(z, k), mat.exposure(weight_pct, held=held), 1.0
    )
    word = "rose" if ret > 0 else "fell"
    body = history_note or (
        f"A one-day move of {abs(z):.1f} standard deviations of its recent volatility. "
        "Check filings and news before reacting."
    )
    return AlertDraft(
        type="move",
        symbol=symbol,
        critical=False,
        materiality=m,
        title=f"{symbol} {word} {abs(ret) * 100:.1f}% (z = {z:+.1f})",
        body=body,
        source_label=f"Market data · {source} · {freshness}",
        dedupe_key=f"move:{symbol}:{_day(now)}",
        suppress_key=f"move:{symbol}",
        payload={
            "z": round(z, 2),
            "k": k,
            "ret_pct": round(ret * 100, 2),
            "sigma_daily_pct": round(sigma * 100, 3),
        },
        event_at=now,
    )


def largest_move_note(ret: float, past_returns: Sequence[tuple[date, float]], today: date) -> str | None:
    """ "Largest one-day gain in about 5 months" when no same-sign move this big happened for 60+ days."""
    bigger = [d for d, r in past_returns if (r > 0) == (ret > 0) and abs(r) >= abs(ret) and d < today]
    if bigger:
        gap_days = (today - max(bigger)).days
        if gap_days < 60:
            return None
        months = max(2, round(gap_days / 30))
    else:
        if not past_returns:
            return None
        months = max(2, round((today - min(d for d, _ in past_returns)).days / 30))
    kind = "gain" if ret > 0 else "decline"
    return f"Largest one-day {kind} in about {months} months."


# ------------------------------------------------------------------ earnings dates
def detect_earnings(
    *,
    symbol: str,
    name: str,
    report_date: date,
    today: date,
    qty: float | None,
    weight_pct: float | None,
    estimated: bool,
    now: datetime,
) -> AlertDraft | None:
    days = (report_date - today).days
    if days < 0 or days > EARNINGS_WINDOW_DAYS:
        return None
    held = qty is not None and qty > 0
    when = (
        "today"
        if days == 0
        else "tomorrow"
        if days == 1
        else f"on {report_date.strftime('%b')} {report_date.day}"
    )
    body = (
        f"You hold {qty:g} shares ({_pct(weight_pct or 0.0)}% of portfolio)."
        if held
        else f"{name} is on your watchlist."
    )
    if estimated:
        body += " The date is estimated from the company's previous results filings."
    m = mat.materiality(
        mat.TYPE_WEIGHTS["earnings"],
        1.0 - days / (EARNINGS_WINDOW_DAYS + 1),
        mat.exposure(weight_pct, held=held),
        1.0,
    )
    return AlertDraft(
        type="earnings",
        symbol=symbol,
        critical=False,
        materiality=m,
        title=f"{symbol} reports earnings {when}",
        body=body,
        source_label="Earnings calendar (estimated)" if estimated else "Earnings calendar",
        dedupe_key=f"earnings:{symbol}:{report_date.isoformat()}",
        suppress_key=None,
        payload={"report_date": report_date.isoformat(), "estimated": estimated},
        event_at=now,
    )
