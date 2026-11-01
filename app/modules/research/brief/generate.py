"""Brief generation (FR-03): ledger in code -> retrieval -> claims (model or mock) -> verify (regenerate <= 2,
then safe template) -> render with code-substituted numbers -> citations. Facts and ledger are identical for
every user; only order, wording depth and plain-language hints come from the style card."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.core.db import Connection
from app.core.pricesource import PriceSource
from app.modules.llm.client import LlmClient
from app.modules.llm.prompts import load_prompt
from app.modules.llm.trace import TraceRecorder
from app.modules.llm.types import LlmRequest, Message
from app.modules.research.brief.catalog import SECTIONS
from app.modules.research.brief.claims import (
    BriefClaims,
    Claim,
    count_unverified_numbers,
    is_notice,
    placeholders_in,
    render_text,
    verify_brief,
)
from app.modules.research.brief.context import BriefContext, Passage, gather_context
from app.modules.research.brief.ledger import Ledger, build_ledger
from app.modules.research.brief.style import SECTION_KEYS, StyleCard
from app.modules.research.brief.templates import build_claims
from app.modules.research.injection import wrap_untrusted
from app.modules.safety.disclaimer import DISCLAIMER

MAX_REGENERATIONS = 2
STALE_10K_DAYS = 15 * 31


@dataclass(slots=True)
class CitationDraft:
    id: UUID
    kind: str  # chunk | xbrl | price
    ref_id: str
    locator: str
    quote_hash: str


@dataclass(slots=True)
class BuiltBrief:
    symbol: str
    as_of: date
    style: StyleCard
    content: dict[str, Any]
    ledger_json: dict[str, Any]
    citations: list[CitationDraft]
    status: str
    stale: bool
    stale_reason: str | None
    verification_attempts: int = 1
    used_safe_template: bool = False
    violations: list[str] = field(default_factory=list)


def build_messages(ledger: Ledger, ctx: BriefContext, style: StyleCard, feedback: list[str]) -> list[Message]:
    prompt = load_prompt("brief_claims")
    lines = [f"{e.id} | {e.label} | {e.display} | {e.source}" for e in ledger.entries.values()]
    docs = []
    for p in ctx.all_passages().values():
        docs.append(wrap_untrusted(p.handle, f"[{p.form} Item {p.item}] {p.text}"))
    order = [k for k in style.order if k != "bb"]
    body = (
        f"Company: {ledger.name} ({ledger.symbol}), fiscal year label {ledger.fy_label}\n"
        f"Style card: {json.dumps(style.model_dump())}\n"
        f"Write sections in this order: {', '.join(order)}; then bull and bear cases.\n"
        "NUMERIC LEDGER (id | label | value | source) - refer to these ONLY as {{id}} placeholders:\n"
        + "\n".join(lines)
        + "\n\nPASSAGES (handles s1..; cite them in citation_ids; cite ledger ids m1.. for figures):\n"
        + "\n".join(docs)
    )
    if feedback:
        body += (
            "\n\nYour previous draft was rejected by the verifier for: "
            + "; ".join(feedback[:8])
            + ". Fix these."
        )
    return [Message("system", prompt.text), Message("user", body)]


def _rows(section: str, ledger: Ledger) -> list[dict[str, str]]:
    keys = {
        "trend": ["revenue", "revenue_growth_pct", "operating_margin_pct"],
        "bal": ["debt_to_equity", "dividend_yield_pct", "fcf"],
        "val": ["price", "eps_diluted", "pe", "pe_percentile"],
    }.get(section, [])
    return [
        {
            "label": ledger.by_key(k).label,
            "value": ledger.by_key(k).display,
            "source": ledger.by_key(k).source,
        }
        for k in keys
    ]


def _stale(ctx: BriefContext, ledger: Ledger, as_of: date) -> tuple[bool, str | None]:
    if ctx.latest_10k is None:
        return True, "No annual report (10-K) has been indexed for this company yet"
    if ctx.latest_10k_filed and (as_of - ctx.latest_10k_filed) > timedelta(days=STALE_10K_DAYS):
        return True, "The latest indexed 10-K is more than 15 months old"
    if ledger.price_freshness == "stale":
        return True, "The price quote is stale; valuation ratios may be out of date"
    if ledger.by_key("price").value is None:
        return True, "No price is available, so valuation ratios are not shown"
    return False, None


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


class _Renderer:
    """Turns verified claims into contract claims + citation rows."""

    def __init__(self, ledger: Ledger, passages: dict[str, Passage]) -> None:
        self.ledger = ledger
        self.passages = passages
        self.chunk_cites: dict[str, CitationDraft] = {}
        self.ledger_cites: dict[str, CitationDraft] = {}
        self.texts: list[str] = []
        self.used_ids: set[str] = set()
        self.n_claims = 0
        self.n_cited = 0

    def _chunk_cite(self, handle: str) -> CitationDraft:
        p = self.passages[handle]
        if handle not in self.chunk_cites:
            self.chunk_cites[handle] = CitationDraft(
                uuid4(), "chunk", p.chunk_id, f"{p.form} Item {p.item} · {p.anchor}", _hash(p.text[:1200])
            )
        return self.chunk_cites[handle]

    def _ledger_cite(self, section: str, ids: list[str]) -> CitationDraft:
        kinds = {self.ledger.entries[i].kind for i in ids}
        kind = "price" if kinds == {"price"} else "xbrl"
        if section not in self.ledger_cites:
            self.ledger_cites[section] = CitationDraft(
                uuid4(),
                kind,
                f"{self.ledger.symbol}:{section}",
                "ledger:" + ",".join(sorted(ids, key=lambda x: int(x[1:]))),
                _hash(",".join(ids)),
            )
        else:  # merge ids cited by later claims of the same section
            cur = self.ledger_cites[section]
            have = set(cur.locator.removeprefix("ledger:").split(","))
            merged = sorted(have | set(ids), key=lambda x: int(x[1:]))
            cur.locator = "ledger:" + ",".join(merged)
            if kind == "xbrl":
                cur.kind = "xbrl"
        return self.ledger_cites[section]

    def claim(self, section: str, c: Claim) -> dict[str, Any]:
        text = render_text(c.text_template, self.ledger)
        refs = placeholders_in(c.text_template)
        for r in c.ledger_refs:
            if r not in refs:
                refs.append(r)
        self.used_ids.update(placeholders_in(c.text_template))
        self.texts.append(text)
        cites: list[str] = []
        ledger_ids = [h for h in c.citation_ids if h.startswith("m")] + [
            r for r in refs if r not in c.citation_ids
        ]
        chunk_handles = [h for h in c.citation_ids if h.startswith("s") and h in self.passages]
        for h in dict.fromkeys(chunk_handles):
            cites.append(str(self._chunk_cite(h).id))
        valid_ids = [i for i in dict.fromkeys(ledger_ids) if i in self.ledger.entries]
        if valid_ids and not is_notice(c):
            cites.append(str(self._ledger_cite(section, valid_ids).id))
        if not is_notice(c):
            self.n_claims += 1
            self.n_cited += 1 if cites else 0
        return {"text": text, "citation_ids": cites, "ledger_refs": refs}

    def all_citations(self) -> list[CitationDraft]:
        return [*self.chunk_cites.values(), *self.ledger_cites.values()]


def assemble(
    symbol: str,
    as_of: date,
    style: StyleCard,
    ledger: Ledger,
    ctx: BriefContext,
    claims: BriefClaims,
    *,
    status: str,
    attempts: int,
    safe: bool,
    violations: list[str],
) -> BuiltBrief:
    r = _Renderer(ledger, ctx.all_passages())
    by_key = {s.key: s for s in claims.sections}
    sections: list[dict[str, Any]] = []
    for key in style.order:
        meta = SECTIONS[key]
        rendered: list[dict[str, Any]] = []
        if key != "bb":
            src = by_key.get(key)
            cl = src.claims if src else []
            rendered = [
                r.claim(key, c)
                for c in (
                    cl
                    or [
                        Claim(
                            text_template="Not enough information was found in the indexed filings for this section.",
                            kind="notice",
                        )
                    ]
                )
            ]
        sections.append({
            "key": key, "title": meta.title, "source_label": meta.source_label, "claims": rendered,
            "rows": _rows(key, ledger),
            "plain_hint": meta.plain_hint if style.jargon == 1 and style.depth != "tldr" else None,
        })  # fmt: skip
    bull = r.claim("bb", claims.bull)
    bear = r.claim("bb", claims.bear)
    unverified = count_unverified_numbers("\n".join(r.texts), ledger, r.used_ids)
    coverage = round(100.0 * r.n_cited / r.n_claims, 1) if r.n_claims else 100.0
    stale, reason = _stale(ctx, ledger, as_of)
    ledger_pub = {
        i: {k: v for k, v in e.as_json().items() if k in ("label", "value", "display", "unit", "source")}
        for i, e in ledger.entries.items()
    }
    content: dict[str, Any] = {
        "symbol": symbol,
        "name": ledger.name,
        "as_of": as_of.isoformat(),
        "status": status,
        "stale": stale,
        "stale_reason": reason,
        "style_card": style.model_dump(),
        "section_order": list(style.order),
        "sections": sections,
        "bull": {"text": bull["text"], "citation_ids": bull["citation_ids"]},
        "bear": {"text": bear["text"], "citation_ids": bear["citation_ids"]},
        "ledger": ledger_pub,
        "citations_summary": {"coverage_pct": coverage, "unverified_numbers": unverified},
        "disclaimer": DISCLAIMER,
    }
    return BuiltBrief(
        symbol=symbol, as_of=as_of, style=style, content=content, ledger_json=ledger.as_json(),
        citations=r.all_citations(), status=status, stale=stale, stale_reason=reason,
        verification_attempts=attempts, used_safe_template=safe, violations=violations,
    )  # fmt: skip


async def generate_brief(
    conn: Connection,
    *,
    user_id: UUID,
    symbol: str,
    as_of: date,
    style: StyleCard,
    llm: LlmClient,
    price_source: PriceSource | None,
    recorder: TraceRecorder,
) -> BuiltBrief:
    with recorder.step("ledger", symbol=symbol) as st:
        ledger, _fs = await build_ledger(conn, symbol, as_of, price_source)
        st.detail["entries"] = len(ledger.entries)
    with recorder.step("retrieve") as st:
        ctx = await gather_context(conn, symbol, as_of)
        st.detail.update(passages=len(ctx.all_passages()), latest_10k=ctx.latest_10k, eightk=len(ctx.eightks))
    valid = set(ctx.all_passages()) | set(ledger.entries)
    prompt = load_prompt("brief_claims")
    feedback: list[str] = []
    claims: BriefClaims | None = None
    status = "ok"
    attempts = 0
    all_violations: list[str] = []
    for attempt in range(MAX_REGENERATIONS + 1):
        attempts = attempt + 1
        resp = await llm.call(
            LlmRequest(
                job="brief_claims",
                messages=build_messages(ledger, ctx, style, feedback),
                data_class="user",
                user_id=user_id,
                response_model=BriefClaims,
                max_tokens=3000,
                prompt_version=prompt.version,
                hints={"ledger": ledger, "context": ctx, "style": style, "feedback": feedback},
            ),
            trace=recorder,
        )
        if resp.degraded or not isinstance(resp.parsed, BriefClaims):
            status = "degraded"
            all_violations.append(f"llm:{resp.degraded_reason}")
            break
        report = verify_brief(resp.parsed, ledger, valid)
        recorder.add(
            "verify",
            0,
            "ok" if report.ok else "error",
            {"attempt": attempts, "violations": report.violations[:10]},
        )
        if report.ok:
            claims = resp.parsed
            break
        feedback = report.violations
        all_violations.extend(report.violations)
    safe = claims is None
    if claims is None:
        status = "degraded"
        claims = build_claims(ledger, ctx, style, safe=True)
        rep = verify_brief(claims, ledger, valid)
        recorder.add("safe_template", 0, "ok" if rep.ok else "error", {"violations": rep.violations[:10]})
    with recorder.step("render"):
        built = assemble(
            symbol,
            as_of,
            style,
            ledger,
            ctx,
            claims,
            status=status,
            attempts=attempts,
            safe=safe,
            violations=all_violations,
        )
    return built


def render_markdown(content: dict[str, Any]) -> str:
    out = [f"# {content['name']} ({content['symbol']}) - research brief as of {content['as_of']}", ""]
    for s in content["sections"]:
        out.append(f"## {s['title']}")
        out.extend(f"- {c['text']}" for c in s["claims"])
        out.append("")
    out += [
        "## Bull / Bear",
        f"- Bull: {content['bull']['text']}",
        f"- Bear: {content['bear']['text']}",
        "",
        f"> {content['disclaimer']}",
    ]
    return "\n".join(out)


def now_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


__all__ = ["SECTION_KEYS", "BuiltBrief", "assemble", "generate_brief", "render_markdown"]
