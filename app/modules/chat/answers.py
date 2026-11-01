"""Deterministic answers: built from tool results with every figure registered in the ledger first.

These are the answers returned in degraded mode / with the MockProvider, and the draft a real model is only
allowed to rephrase (it may not add or change a figure - see `narrate.py`).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.modules.chat.glossary import GLOSSARY, HELP, help_key
from app.modules.chat.ledger import ChatLedger
from app.modules.chat.metrics import MetricResult, register
from app.modules.edgar.ratios import fiscal_year_label
from app.modules.research.embedding import tokenize
from app.modules.research.injection import HeuristicScanner
from app.modules.research.retrieval import RetrievedChunk
from app.modules.safety import check_output

_SCANNER = HeuristicScanner()
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
_HAS_DIGIT = re.compile(r"\d")
#: words that carry no topic signal for the "is this in the filing?" test
WEAK_TERMS = frozenset(
    [
        "risk",
        "risks",
        "factor",
        "factors",
        "main",
        "key",
        "major",
        "company",
        "companies",
        "filing",
        "filings",
        "annual",
        "report",
        "say",
        "says",
        "said",
        "mention",
        "mentions",
        "discuss",
        "discusses",
        "disclose",
        "disclosed",
        "about",
        "tell",
        "show",
        "describe",
        "describes",
        "does",
        "did",
        "have",
        "has",
        "many",
        "much",
        "year",
        "years",
        "latest",
        "recent",
        "describe",
        "business",
        "item",
        "items",
        "form",
        "form10k",
        "10k",
        "8k",
        "its",
        "their",
        "what",
        "which",
        "who",
        "when",
        "where",
        "why",
        "how",
        "are",
        "was",
        "were",
        "the",
        "and",
        "for",
        "from",
        "with",
        "into",
        "over",
        "this",
        "that",
        "these",
        "those",
        "me",
        "you",
        "your",
        "can",
        "could",
        "would",
        "please",
        "give",
        "summary",
        "summarize",
        "summarise",
        "overview",
        "anything",
        "something",
        "know",
    ]
)

NOT_FOUND = "I couldn't find that in the filings I have indexed or in your portfolio data, so I won't guess. "


@dataclass(slots=True)
class Draft:
    text: str
    ledger: ChatLedger = field(default_factory=ChatLedger)
    citations: list[dict[str, Any]] = field(default_factory=list)
    grounded: bool = True
    passages: list[tuple[str, str]] = field(default_factory=list)  # (citation id, sentence) for the model


def stem(t: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def content_terms(message: str, company_tokens: set[str]) -> list[str]:
    out: list[str] = []
    for raw in tokenize(message):
        t = re.sub(r"['\u2019]s$", "", raw)
        if t in WEAK_TERMS or t in company_tokens or any(ch.isdigit() for ch in t):
            continue
        s = stem(t)
        if s not in out:
            out.append(s)
    return out


def required_matches(n_terms: int) -> int:
    return 0 if n_terms == 0 else max(1, math.ceil(0.5 * n_terms))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(" ".join(text.split())) if s.strip()]


def _looks_like_heading(sent: str) -> bool:
    """Run-in section headings ("Climate Change and the Energy Transition Net-zero scenarios.")."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", sent) if len(w) > 3]
    return len(words) >= 4 and sum(w[0].isupper() for w in words) / len(words) >= 0.6


def pick_sentences(
    chunks: list[RetrievedChunk], terms: list[str], limit: int = 3
) -> list[tuple[RetrievedChunk, str, int]]:
    """Digit-free, scanner-clean, guardrail-clean sentences ranked by distinct query-term matches.

    With no content terms (open question) the leading sentences of the best-ranked chunks are returned.
    """
    need = required_matches(len(terms))
    scored: list[tuple[int, int, RetrievedChunk, str]] = []
    for ci, ch in enumerate(chunks):
        for si, sent in enumerate(_sentences(ch.text)):
            if not 40 <= len(sent) <= 320 or _HAS_DIGIT.search(sent):
                continue
            if _looks_like_heading(sent) or _SCANNER.scan(sent).flagged or check_output(sent).blocked:
                continue
            stems = {stem(t) for t in tokenize(sent)}
            hits = sum(1 for t in terms if t in stems)
            if need and hits < need:
                continue
            scored.append((hits, -(ci * 100 + si), ch, sent))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked: list[tuple[RetrievedChunk, str, int]] = []
    seen: set[str] = set()
    per_chunk: dict[str, int] = {}
    for hits, _, ch, sent in scored:
        if sent in seen or per_chunk.get(ch.id, 0) >= 2:
            continue
        seen.add(sent)
        per_chunk[ch.id] = per_chunk.get(ch.id, 0) + 1
        picked.append((ch, sent, hits))
        if len(picked) >= limit:
            break
    return picked


# ------------------------------------------------------------------------------------------- portfolio
def _money(x: float | None) -> str:
    return "n/m" if x is None else f"${x:,.2f}"


def _signed_money(x: float | None) -> str:
    if x is None:
        return "n/m"
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _pct(x: float | None, nd: int = 1) -> str:
    return "n/m" if x is None else f"{x:.{nd}f}%"


def _signed_pct(x: float | None) -> str:
    return "n/m" if x is None else f"{'+' if x >= 0 else '-'}{abs(x):.1f}%"


_PORTFOLIO_CITE = {
    "id": "portfolio:paper",
    "kind": "portfolio",
    "label": "Your paper portfolio",
    "doc_label": "Positions, cash and prices from your paper ledger",
    "route": "/portfolio",
}
_RISK_CITE = {
    "id": "risk:engine",
    "kind": "risk_engine",
    "label": "Risk engine",
    "doc_label": "Deterministic risk engine (your rules)",
    "route": "/portfolio",
}


def no_portfolio() -> Draft:
    return Draft(
        "You don't have a paper portfolio yet, so there are no holdings, cash or rules to report. "
        "Finish onboarding to create one.",
        grounded=False,
    )


def portfolio_overview(pf: dict[str, Any]) -> Draft:
    L = ChatLedger()
    positions = pf.get("positions") or []
    if not positions:
        n0 = L.add("Number of positions", "0", source="portfolio:positions", value=0)
        eq0 = L.add("Equity", _money(pf.get("equity")), source="portfolio:equity", value=pf.get("equity"))
        text = (
            f"You don't hold any positions yet ({n0} positions). Your account is all cash: equity is {eq0}."
        )
        return Draft(text, L, [dict(_PORTFOLIO_CITE)])
    n = L.add("Number of positions", str(len(positions)), source="portfolio:positions", value=len(positions))
    eq = L.add("Equity", _money(pf["equity"]), source="portfolio:equity", value=pf["equity"])
    cash = L.add("Cash", _money(pf["cash"]), source="portfolio:cash", value=pf["cash"])
    cash_pct = (pf["cash"] / pf["equity"] * 100) if pf["equity"] else 0.0
    cp = L.add("Cash share of equity", _pct(cash_pct), source="portfolio:cash/equity", value=cash_pct)
    day = L.add(
        "Day change", _signed_money(pf["day_change"]), source="portfolio:day_change", value=pf["day_change"]
    )
    dayp = L.add("Day change %", _signed_pct(pf["day_change_pct"]), source="portfolio:day_change_pct", value=pf["day_change_pct"])  # fmt: skip
    top = []
    for p in positions[:3]:
        w = L.add(
            f"{p['symbol']} weight",
            _pct(p["weight_pct"]),
            source="portfolio:weight_pct",
            value=p["weight_pct"],
        )
        top.append(f"{p['symbol']} {w}")
    text = (
        f"You hold {n} positions. Total equity is {eq}, of which {cash} is cash ({cp} of equity), and the day "
        f"change is {day} ({dayp}). Your largest holdings are {', '.join(top)} (share of equity)."
    )
    return Draft(text, L, [dict(_PORTFOLIO_CITE)])


def portfolio_concentration(pf: dict[str, Any], risk: dict[str, Any]) -> Draft:
    L = ChatLedger()
    positions = pf.get("positions") or []
    if not positions:
        n0 = L.add("Number of positions", "0", source="portfolio:positions", value=0)
        return Draft(
            f"You don't hold any positions yet ({n0} positions), so there is no concentration to measure.",
            L, [dict(_PORTFOLIO_CITE)],
        )  # fmt: skip
    m = risk["metrics"]
    rules = {r["key"]: r for r in risk["rules"]}
    n = L.add("Number of positions", str(len(positions)), source="portfolio:positions", value=len(positions))
    hhi = L.add("HHI", "n/m" if m["hhi"] is None else f"{m['hhi']:.2f}", source="risk:hhi", value=m["hhi"])
    effn = L.add("Effective number of holdings", "n/m" if m["effective_n"] is None else f"{m['effective_n']:.1f}", source="risk:effective_n", value=m["effective_n"])  # fmt: skip
    top = positions[0]
    tw = L.add(
        f"{top['symbol']} weight",
        _pct(top["weight_pct"]),
        source="portfolio:weight_pct",
        value=top["weight_pct"],
    )
    pos_rule, sec_rule = rules["pos"], rules["sec"]
    pcap = L.add("Position cap", _pct(pos_rule["limit"], 0 if float(pos_rule["limit"]).is_integer() else 1), source="rules:pos", value=pos_rule["limit"])  # fmt: skip
    text = (
        f"Your portfolio's concentration index (HHI) is {hhi}, which is like holding about {effn} equally sized "
        f"positions (you hold {n}). Your largest position is {top['symbol']} at {tw} of equity against your "
        f"position cap of {pcap}."
    )
    sectors = pf.get("sectors") or []
    if sectors:
        s0 = sectors[0]
        sw = L.add(f"{s0['sector']} weight", _pct(s0["weight_pct"]), source="portfolio:sector_weight_pct", value=s0["weight_pct"])  # fmt: skip
        scap = L.add("Sector cap", _pct(sec_rule["limit"], 0 if float(sec_rule["limit"]).is_integer() else 1), source="rules:sec", value=sec_rule["limit"])  # fmt: skip
        text += f" The largest sector is {s0['sector']} at {sw} against your sector cap of {scap}."
    breaches = []
    for key, name in (("pos", "position"), ("sec", "sector")):
        r = rules[key]
        if r["status"] == "breach":
            over = L.add(f"{name.title()} cap overshoot (pp)", f"{r['current'] - r['limit']:.1f}", source=f"rules:{key}", value=r["current"] - r["limit"])  # fmt: skip
            breaches.append(f"the {name} cap is exceeded by {over} percentage points")
    text += (" Right now " + " and ".join(breaches) + ".") if breaches else " Both are within your rules."
    return Draft(text, L, [dict(_PORTFOLIO_CITE), dict(_RISK_CITE)])


_RULE_LABEL = {
    "pos": "position cap",
    "sec": "sector cap",
    "dd": "drawdown limit",
    "trades": "trades in the past week",
    "cash": "minimum cash",
}
_STATUS_TEXT = {"ok": "within the limit", "warn": "close to the limit", "breach": "outside the limit"}


def portfolio_risk(pf: dict[str, Any], risk: dict[str, Any]) -> Draft:
    L = ChatLedger()
    m = risk["metrics"]
    parts: list[str] = []
    for r in risk["rules"]:
        key = r["key"]
        integer = key == "trades"
        cur = L.add(f"{_RULE_LABEL[key]} current", f"{r['current']:.0f}" if integer else _pct(r["current"]), source=f"risk:rules:{key}", value=r["current"])  # fmt: skip
        lim = L.add(f"{_RULE_LABEL[key]} limit", f"{r['limit']:.0f}" if integer else _pct(r["limit"], 0 if float(r["limit"]).is_integer() else 1), source=f"rules:{key}", value=r["limit"])  # fmt: skip
        subj = f" ({r['subject']})" if r.get("subject") else ""
        parts.append(f"{_RULE_LABEL[key]}{subj}: {cur} against {lim}, {_STATUS_TEXT[r['status']]}")
    text = "Against your rules - " + "; ".join(parts) + "."
    stats: list[str] = []
    for key, label, val in (
        ("vol_60d", "recent volatility (annualised)", m.get("vol_60d")),
        ("beta", "beta vs the market", m.get("beta")),
        ("max_drawdown_pct", "maximum drawdown", m.get("max_drawdown_pct")),
    ):
        if val is None:
            stats.append(f"{label} is not available yet (not enough price history)")
        else:
            disp = f"{val:.2f}" if key == "beta" else _pct(val)
            stats.append(f"{label} is {L.add(label, disp, source=f'risk:{key}', value=val)}")
    text += " " + "; ".join(stats).capitalize() + "."
    nb = len([r for r in risk["rules"] if r["status"] == "breach"])
    if nb:
        text += f" {L.add('Rules outside the limit', str(nb), source='risk:rules', value=nb)} of your rules are currently outside the limit."  # fmt: skip
    return Draft(text, L, [dict(_PORTFOLIO_CITE), dict(_RISK_CITE)])


# ------------------------------------------------------------------------------------------- reference
def glossary_answer(key: str) -> Draft:
    return Draft(GLOSSARY[key], grounded=True, citations=[{"id": f"glossary:{key}", "kind": "glossary", "label": "StockPilot glossary", "route": "/ask"}])  # fmt: skip


def help_answer(message: str) -> Draft:
    return Draft(HELP[help_key(message)], grounded=True, citations=[{"id": "help:app", "kind": "help", "label": "Using StockPilot", "route": "/profile"}])  # fmt: skip


def not_found(reason: str = "no_source", n_universe: int | None = None) -> Draft:
    L = ChatLedger()
    text = NOT_FOUND
    if reason == "no_company":
        text += "I don't see a company from the supported universe in your question. "
    if n_universe:
        n = L.add("Companies in the supported universe", str(n_universe), source="universe", value=n_universe)
        text += f"I can answer questions about your portfolio and rules, and about disclosures and reported figures for the {n} companies in the supported universe."  # fmt: skip
    else:
        text += "I can answer questions about your portfolio and rules, and about disclosures and reported figures for the supported companies."  # fmt: skip
    return Draft(text, L, [], grounded=False)


# ------------------------------------------------------------------------------------------- filings
_YEAR = re.compile(r"\b(?:1\d|20)\d{2}\b")


def metric_answer(
    results: list[MetricResult], names: dict[str, str], ciks: dict[str, str | None], message: str = ""
) -> Draft:
    L = ChatLedger()
    lines: list[str] = []
    cites: list[dict[str, Any]] = []
    years = {int(y) for y in _YEAR.findall(message)}
    for res in results:
        name = names.get(res.symbol, res.symbol)
        if res.value is None:
            lines.append(f"{name}: {res.label} isn't available in the XBRL data I have ingested (n/m).")
            continue
        fy = fiscal_year_label(res.period_end)
        if years and res.period_end and years != {res.period_end.year}:
            # only the latest fiscal year is looked up: never answer a different year with this year's number
            lines.append(f"{name}: I can only report the latest fiscal year in the indexed filings ({fy}), not the year you asked about.")  # fmt: skip
            continue
        disp = register(L, res, fy)
        cite_no = len(cites) + 1
        end = f" (period ending {res.period_end.isoformat()})" if res.period_end else ""
        acc = (
            f", from SEC filing {res.accession_nos[0]}"
            if res.accession_nos
            else ", computed by code from XBRL facts"
        )
        exact = f" ({res.exact})" if res.exact else ""
        verb = "is" if res.key in ("revenue_growth_pct", "operating_margin_pct", "debt_to_equity", "pe", "dividend_yield_pct") else "was"  # fmt: skip
        lines.append(f"{name}'s {res.label.lower()} {verb} {disp}{exact} for {fy}{end}{acc}. [{cite_no}]")
        cik = ciks.get(res.symbol)
        cites.append(
            {
                "id": f"xbrl:{res.symbol}:{res.key}:{res.accession_nos[0] if res.accession_nos else 'code'}",
                "kind": "xbrl",
                "n": cite_no,
                "symbol": res.symbol,
                "label": f"{res.symbol} {res.label} {fy} ({res.source})",
                "doc_label": "SEC XBRL company facts",
                "accession_no": res.accession_nos[0] if res.accession_nos else None,
                "period_end": res.period_end.isoformat() if res.period_end else None,
                "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json" if cik else None,
                "route": f"/brief/{res.symbol}",
            }
        )
    grounded = bool(cites)
    if not grounded:
        d = not_found("no_source")
        d.text = " ".join(lines) + " " + d.text
        return d
    return Draft("\n".join(lines), L, cites, grounded=True)


def filing_text_answer(picked: list[tuple[RetrievedChunk, str, int]], names: dict[str, str]) -> Draft:
    if not picked:
        return not_found("no_source")
    L = ChatLedger()
    lines: list[str] = []
    cites: list[dict[str, Any]] = []
    passages: list[tuple[str, str]] = []
    by_chunk: dict[str, int] = {}
    for ch, sent, _ in picked:
        if ch.id not in by_chunk:
            by_chunk[ch.id] = len(cites) + 1
            filed = ch.filed_at.date().isoformat() if ch.filed_at else None
            item = f" Item {ch.item}" if ch.item else ""
            cites.append(
                {
                    "id": f"chunk:{ch.id}",
                    "kind": "chunk",
                    "n": by_chunk[ch.id],
                    "chunk_id": ch.id,
                    "symbol": ch.symbol,
                    "label": f"{ch.symbol} {ch.form or 'filing'}{item}",
                    "doc_label": f"{names.get(ch.symbol, ch.symbol)} {ch.form or 'filing'}{item}",
                    "form": ch.form,
                    "item": ch.item,
                    "accession_no": ch.accession_no,
                    "filed_at": filed,
                    "excerpt": sent,
                    "route": f"/brief/{ch.symbol}",
                }
            )
        lines.append(f'"{sent}" [{by_chunk[ch.id]}]')
        passages.append((f"chunk:{ch.id}", sent))
    first = picked[0][0]
    item = f", Item {first.item}" if first.item else ""
    filed = f" (filed {first.filed_at.date().isoformat()})" if first.filed_at else ""
    head = f"From {names.get(first.symbol, first.symbol)}'s {first.form or 'filing'}{filed}{item}:"
    tail = "These passages are quoted from the filing; open the cited sections for context."
    return Draft(head + "\n" + "\n".join(lines) + "\n" + tail, L, cites, grounded=True, passages=passages)
