"""The chat pipeline (BE-19):

1. input guard   - safety input classifier; advice / guarantee / prediction / price-target / injection asks are
                   refused with an educational redirect and never reach a tool or a model;
2. routing       - intent chosen in code (`intents.route`), never by the model;
3. tools         - read-only: portfolio + risk engine, XBRL metrics, filing retrieval; every figure goes into the ledger;
4. draft         - deterministic answer from the tool results ("I couldn't find that" when the sources lack it);
5. narration     - the LLM (Standard mode) may only rephrase the draft; rejected if it changes a figure;
6. output guard  - `safety.check_output` on the final text.
Every stage is a trace step (`TraceRecorder`) so the Audit screen can show exactly what ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.errors import AppError, ErrorCode
from app.core.pricesource import PriceSource
from app.modules.chat import answers, tools
from app.modules.chat.answers import Draft
from app.modules.chat.intents import Route, route
from app.modules.chat.ledger import ChatLedger
from app.modules.chat.narrate import narrate
from app.modules.chat.symbols import Company, load_universe
from app.modules.llm.client import LlmClient
from app.modules.llm.trace import TraceRecorder
from app.modules.paper.context import EngineContext
from app.modules.safety import check_output, classify_input, refusal_for

SAFE_FALLBACK = (
    "I couldn't put together an answer that meets StockPilot's wording rules, so I'm not showing it. "
    "You can rephrase the question or open the cited sections directly."
)


@dataclass(slots=True)
class ChatResult:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    grounded: bool = False
    intent: str = "refused"
    ledger: ChatLedger = field(default_factory=ChatLedger)
    narration: str = "n/a"


async def _draft_for(
    conn: Connection,
    r: Route,
    message: str,
    universe: list[Company],
    *,
    user_id: UUID | None,
    ctx: EngineContext | None,
    price_source: PriceSource | None,
    as_of: date,
    rec: TraceRecorder,
) -> Draft:
    if r.intent in ("portfolio_overview", "portfolio_concentration", "portfolio_risk"):
        if user_id is None or ctx is None:
            raise RuntimeError("portfolio questions need a user and an engine context")
        try:
            pf = await tools.get_portfolio(conn, user_id, ctx, rec)
        except AppError as exc:
            if exc.code is not ErrorCode.NOT_FOUND:
                raise
            return answers.no_portfolio()
        if r.intent == "portfolio_overview":
            return answers.portfolio_overview(pf)
        risk = await tools.evaluate_risk(conn, user_id, ctx, rec)
        if r.intent == "portfolio_concentration":
            return answers.portfolio_concentration(pf, risk)
        return answers.portfolio_risk(pf, risk)
    if r.intent == "glossary" and r.glossary_key:
        return answers.glossary_answer(r.glossary_key)
    if r.intent == "app_help":
        return answers.help_answer(message)
    names = {c.symbol: c.name for c in universe}
    if r.intent == "filing_metric":
        results = await tools.get_xbrl_metrics(conn, r.companies, r.metrics, as_of, price_source, rec)
        ciks = {
            row["symbol"]: row["cik"]
            for row in await conn.fetch(
                "select symbol, cik from tickers where symbol = any($1::text[])",
                [c.symbol for c in r.companies],
            )
        }
        return answers.metric_answer(results, names, ciks, message)
    if r.intent == "filing_text":
        picked: list[Any] = []
        for c in r.companies:
            chunks, terms = await tools.search_filings(conn, c, message, r.items, as_of, rec)
            picked.extend(tools.sentences_for(chunks, terms)[: 3 if len(r.companies) == 1 else 2])
        return answers.filing_text_answer(picked, names)
    return answers.not_found("no_company" if not r.companies else "no_source", len(universe))


async def respond(
    conn: Connection,
    message: str,
    rec: TraceRecorder,
    llm: LlmClient,
    *,
    user_id: UUID | None,
    ctx: EngineContext | None,
    price_source: PriceSource | None,
    as_of: date,
) -> ChatResult:
    """Run the pipeline for one question. Persistence is the caller's job (see `router`)."""
    with rec.step("Input safety check", classifier="regex-baseline (Llama Guard 4 hook)") as st:
        verdict = classify_input(message)
        st.detail["summary"] = (
            f"refused: {refusal_for(verdict.category).reason}" if verdict.refuse else "safe"
        )
        if verdict.refuse:
            st.outcome = "blocked"
    if verdict.refuse:
        ref = refusal_for(verdict.category)
        rec.name = f"chat:refused:{verdict.category}"
        with rec.step("Refusal template + educational redirect") as st:
            guard = check_output(ref.text)
            st.detail["summary"] = "No guarantee phrases" if not guard.blocked else "template blocked"
        rec.set_outcome("blocked")
        return ChatResult(ref.text, [], True, ref.reason, False, "refused")

    universe = await load_universe(conn)
    with rec.step("Intent routing (code)") as st:
        r = route(message, universe)
        st.detail["summary"] = r.intent
        st.detail["companies"] = [c.symbol for c in r.companies]
    rec.name = f"chat:{r.intent}"

    draft = await _draft_for(
        conn, r, message, universe, user_id=user_id, ctx=ctx, price_source=price_source, as_of=as_of, rec=rec
    )
    text, note = draft.text, "n/a"
    if draft.grounded:
        n = await narrate(llm, message, draft, user_id=user_id, rec=rec) if user_id is not None else None
        if n is not None:
            text, note = n.text, n.note
            rejected = n.note.startswith("rejected")
            rec.add(
                "Narration verifier",
                0,
                "blocked" if rejected else "ok",
                {
                    "summary": "model wording accepted"
                    if n.used_model
                    else f"deterministic draft used ({n.note})"
                },
            )
    refused, reason = False, None
    with rec.step("Numeric verifier") as st:
        bad = draft.ledger.unverified(text)
        if bad and text != draft.text:
            text, note = draft.text, f"verifier:{len(bad)}"
            bad = draft.ledger.unverified(text)
        st.detail["summary"] = f"{len(draft.ledger)} ledger figures, {len(bad)} unverified"
        if bad:  # a template produced a figure the tools did not: fail closed, never show it
            st.outcome = "blocked"
            text, refused, reason = SAFE_FALLBACK, True, "unverified_numbers"
            rec.set_outcome("blocked")
    with rec.step("Output guardrail") as st:
        out = check_output(text)
        st.detail["summary"] = "clean" if not out.blocked else "blocked: " + ", ".join(sorted(out.categories))
        if out.blocked:
            st.outcome = "blocked"
            text, refused, reason = SAFE_FALLBACK, True, "output_guardrail"
            rec.set_outcome("blocked")
    return ChatResult(text, draft.citations if not refused else [], refused, reason,
                      draft.grounded and not refused, r.intent, draft.ledger, note)  # fmt: skip
