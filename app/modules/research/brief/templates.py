"""Code-authored claim templates, extractive sentence selection, and the deterministic mock "model".

* `numeric_claims()` - sentences built from ledger placeholders only (used by the mock and by the safe template).
* `select_sentences()` - extractive selection of digit-free sentences from retrieved passages (mock mode).
* `mock_brief_claims()` - registered as the MockProvider handler for job `brief_claims`: reads the structured
  `hints` the caller passes (ledger, passages, style) and returns the same JSON a live model would.
"""

from __future__ import annotations

import re

from app.modules.edgar.ratios import ordinal  # noqa: F401  (re-exported for callers building labels)
from app.modules.llm.providers.mock import register_mock_handler
from app.modules.llm.types import ProviderRequest
from app.modules.research.brief.catalog import SECTIONS
from app.modules.research.brief.claims import (
    BriefClaims,
    Claim,
    SectionClaims,
    literal_numbers,
)
from app.modules.research.brief.context import BriefContext, Passage
from app.modules.research.brief.ledger import NM, Ledger
from app.modules.research.brief.style import SECTION_KEYS, StyleCard
from app.modules.research.embedding import tokenize
from app.modules.safety.guardrail import check_output

NOT_ENOUGH = "Not enough information was found in the indexed filings for this section."
NO_NUMBERS = "Figures for this section are not available in the indexed filings."
NOT_MEANINGFUL = "Free cash flow and debt to equity are not meaningful measures for banks."
NO_PEERS = "A comparison with peers is not available for this company yet."
NO_8K = "No recent Form 8-K filings were found in the indexed filings."

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_BAD_SENT = re.compile(
    r"table of contents|annual report|form 10-k|\bpage\b|incorporated (?:herein )?by reference", re.IGNORECASE
)
_PER_SECTION = {"tldr": 1, "standard": 2, "deep": 4}


def notice(text: str) -> Claim:
    return Claim(text_template=text, kind="notice")


def _avail(ledger: Ledger, *keys: str) -> list[str]:
    return [k for k in keys if ledger.by_key(k).value is not None]


def _cite(ledger: Ledger, *keys: str) -> tuple[list[str], list[str]]:
    ids = [ledger.by_key(k).id for k in keys]
    return ids, ids


def numeric_claims(section: str, ledger: Ledger, style: StyleCard) -> list[Claim]:
    """Deterministic ledger-only sentences; plain wording for beginners (jargon level 1)."""
    plain = style.jargon == 1
    L = ledger.by_key

    def claim(text: str, *keys: str) -> Claim:
        ids, refs = _cite(ledger, *keys)
        return Claim(text_template=text, ledger_refs=refs, citation_ids=ids)

    if section == "trend":
        if L("revenue").value is None:
            return [notice(NO_NUMBERS)]
        rev, gr, om = L("revenue"), L("revenue_growth_pct"), L("operating_margin_pct")
        keys = ["revenue"]
        if plain:
            text = f"In {ledger.fy_label} the company brought in {{{{{rev.id}}}}} in revenue"
        else:
            text = f"Revenue was {{{{{rev.id}}}}} in {ledger.fy_label}"
        if gr.value is not None:
            text += f", {{{{{gr.id}}}}} compared with the prior fiscal year"
            keys.append("revenue_growth_pct")
        if om.value is not None:
            text += (
                f", and it kept {{{{{om.id}}}}} of each sales dollar as operating profit"
                if plain
                else f", with an operating margin of {{{{{om.id}}}}}"
            )
            keys.append("operating_margin_pct")
        return [claim(text + ".", *keys)]
    if section == "bal":
        keys = _avail(ledger, "debt_to_equity", "fcf", "dividend_yield_pct")
        out: list[Claim] = []
        if ledger.sector == "Financials":
            out.append(notice(NOT_MEANINGFUL))
        if not keys and not out:
            return [notice(NO_NUMBERS)]
        bits = []
        if L("debt_to_equity").value is not None:
            bits.append(f"debt to equity was {{{{{L('debt_to_equity').id}}}}}")
        if L("fcf").value is not None:
            bits.append(f"free cash flow was {{{{{L('fcf').id}}}}}")
        if L("dividend_yield_pct").value is not None:
            bits.append(f"the dividend yield was {{{{{L('dividend_yield_pct').id}}}}}")
        if bits:
            text = (
                "Based on the latest filings, "
                + ", ".join(bits[:-1])
                + (" and " if len(bits) > 1 else "")
                + bits[-1]
                + "."
            )
            out.append(claim(text, *keys))
        return out
    if section == "val":
        out = []
        if L("pe").value is not None:
            text = (
                f"At a price of {{{{{L('price').id}}}}} and diluted earnings per share of {{{{{L('eps_diluted').id}}}}}, "
                f"the trailing P/E is {{{{{L('pe').id}}}}}."
            )
            out.append(claim(text, "price", "eps_diluted", "pe"))
        else:
            out.append(notice(NO_NUMBERS))
        if L("pe_percentile").value is not None:
            out.append(
                claim(
                    f"That P/E sits at the {{{{{L('pe_percentile').id}}}}} percentile of its peer set.",
                    "pe_percentile",
                    "pe",
                )
            )
        gp, mp = L("growth_percentile"), L("margin_percentile")
        if gp.value is not None and mp.value is not None:
            out.append(
                claim(
                    f"Revenue growth ranks at the {{{{{gp.id}}}}} percentile and operating margin at the {{{{{mp.id}}}}} percentile of the peer set.",
                    "growth_percentile",
                    "margin_percentile",
                )
            )
        elif not out[1:]:
            out.append(notice(NO_PEERS))
        return out
    return []


_STOP_BULLET = re.compile(r"^\W+")
_STARTERS = frozenset(
    [
        "The",
        "We",
        "Our",
        "In",
        "As",
        "A",
        "An",
        "Certain",
        "Many",
        "Some",
        "If",
        "Any",
        "These",
        "This",
        "Those",
        "Such",
        "Although",
        "While",
        "Because",
        "When",
    ]
)
_GLUE = frozenset({"The", "We", "Our", "Certain", "Many"})


def looks_like_heading_glue(s: str) -> bool:
    """ "Competition The market ..." / "Graphics revenue - The year ...": a heading fused to its first sentence."""
    t = s.split()
    if "\u2022" in s or any(x in (" - ", " \u2013 ") for x in (s[i : i + 3] for i in range(len(s) - 2))):
        return True
    if t and t[0] not in _STARTERS:
        for i in range(1, min(5, len(t))):
            if t[i] in _GLUE and all(w[:1].isupper() for w in t[:i]):
                return True
    return False


def select_sentences(passages: list[Passage], query: str, k: int) -> list[tuple[str, str]]:
    """(sentence, passage handle) - digit-free, quotable, relevant; deterministic order."""
    q = set(tokenize(query))
    scored: list[tuple[float, int, str, str]] = []
    seen: set[str] = set()
    for pi, p in enumerate(passages):
        for si, raw in enumerate(_SENT.split(p.text.replace("\n", " "))):
            s = _STOP_BULLET.sub("", raw.strip())
            if not (60 <= len(s) <= 320) or not s[0].isupper() or not s.endswith("."):
                continue
            if literal_numbers(s) or re.search(r"[{}<>$%]|\d", strip_dates(s)) or _BAD_SENT.search(s):
                continue
            if check_output(s).blocked or s.lower() in seen or looks_like_heading_glue(s):
                continue
            toks = set(tokenize(s))
            overlap = len(q & toks)
            if overlap == 0:
                continue
            seen.add(s.lower())
            scored.append((overlap / (len(toks) ** 0.5), -(pi * 100 + si), s, p.handle))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [(s, h) for _, _, s, h in scored[:k]]


def strip_dates(s: str) -> str:
    """Removes allow-listed year tokens so a stray digit check can ignore them."""
    return re.sub(r"\b(?:19|20)\d{2}\b", " ", s)


def extractive_claims(section: str, ctx: BriefContext, style: StyleCard) -> list[Claim]:
    meta = SECTIONS[section]
    passages = ctx.passages.get(section, [])
    claims: list[Claim] = []
    n = _PER_SECTION[style.depth]
    if section == "cat":
        for e in ctx.eightks[: (1 if style.depth == "tldr" else 3)]:
            items = e.title.split("Items", 1)[1].strip(" ()") if "Items" in e.title else ""
            n_items = len([x for x in items.split(",") if x.strip()])
            where = (
                f" covering {'Items' if n_items > 1 else 'Item'} {items.replace(',', ', ')}" if items else ""
            )
            claims.append(
                Claim(
                    text_template=f"The company filed a Form 8-K on {e.filed_at.isoformat()}{where}.",
                    citation_ids=[e.handle],
                )
            )
        if not claims:
            claims.append(notice(NO_8K))
        n = max(0, n - len(claims))
        passages = [p for p in passages if p.form == "10-K"]
    if n:
        for sent, handle in select_sentences(passages, meta.query, n):
            claims.append(Claim(text_template=sent, citation_ids=[handle]))
    if not claims:
        claims.append(notice(NOT_ENOUGH))
    return claims


def section_claims(
    section: str, ledger: Ledger, ctx: BriefContext, style: StyleCard, *, safe: bool = False
) -> list[Claim]:
    numeric = numeric_claims(section, ledger, style)
    if section in ("val",):
        out = numeric
    elif section in ("trend", "bal"):
        out = numeric + ([] if safe else _first(extractive_claims(section, ctx, style), style, tail=True))
    elif safe:
        out = (
            [notice(NOT_ENOUGH)]
            if section != "cat"
            else (extractive_claims("cat", ctx, style)[:1] if ctx.eightks else [notice(NO_8K)])
        )
    else:
        out = extractive_claims(section, ctx, style)
    if style.depth == "tldr":
        out = out[:1]
    return out


def _first(claims: list[Claim], style: StyleCard, tail: bool) -> list[Claim]:
    """Qualitative sentence(s) appended to numeric sections; dropped when only a notice exists."""
    facts = [c for c in claims if c.kind == "fact"]
    return facts[: (1 if style.depth != "deep" else 2)] if tail else facts


def bull_bear(
    ledger: Ledger, ctx: BriefContext, style: StyleCard, *, safe: bool = False
) -> tuple[Claim, Claim]:
    gr, om = ledger.by_key("revenue_growth_pct"), ledger.by_key("operating_margin_pct")
    if gr.value is not None and om.value is not None:
        bull = Claim(
            text_template=f"For this case to hold, revenue growth (currently {{{{{gr.id}}}}}) has to continue and the operating margin (currently {{{{{om.id}}}}}) has to hold up.",
            ledger_refs=[gr.id, om.id], citation_ids=[gr.id, om.id],
        )  # fmt: skip
    elif gr.value is not None:
        bull = Claim(
            text_template=f"For this case to hold, revenue growth (currently {{{{{gr.id}}}}}) has to continue.",
            ledger_refs=[gr.id],
            citation_ids=[gr.id],
        )
    else:
        bull = notice(NOT_ENOUGH)
    bear = notice(NOT_ENOUGH)
    risks = [] if safe else select_sentences(ctx.passages.get("risk", []), SECTIONS["risk"].query, 1)
    if risks:
        sent, handle = risks[0]
        bear = Claim(
            text_template=f"The company itself lists risks such as this: {sent[0].lower()}{sent[1:]}",
            citation_ids=[handle],
        )
    elif om.value is not None:
        bear = Claim(
            text_template=f"This case is what happens if the operating margin of {{{{{om.id}}}}} comes under pressure.",
            ledger_refs=[om.id], citation_ids=[om.id],
        )  # fmt: skip
    return bull, bear


def build_claims(ledger: Ledger, ctx: BriefContext, style: StyleCard, *, safe: bool = False) -> BriefClaims:
    sections = [
        SectionClaims(key=k, claims=section_claims(k, ledger, ctx, style, safe=safe))
        for k in SECTION_KEYS
        if k != "bb"
    ]
    bull, bear = bull_bear(ledger, ctx, style, safe=safe)
    return BriefClaims(sections=sections, bull=bull, bear=bear)


def mock_brief_claims(req: ProviderRequest) -> str:
    """Deterministic stand-in for the model: same JSON schema, extractive wording, placeholders for figures."""
    h = req.hints
    claims = build_claims(h["ledger"], h["context"], h["style"], safe=False)
    return claims.model_dump_json()


register_mock_handler("brief_claims", mock_brief_claims)
__all__ = ["NM", "build_claims", "mock_brief_claims", "numeric_claims", "section_claims", "select_sentences"]
