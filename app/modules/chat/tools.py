"""Read-only tools the chat pipeline may call (SPEC 7.4): get_portfolio, evaluate_risk, get_xbrl_metric,
search_filings. None of them can write, place an order or create a memory. Each records a trace step."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.pricesource import PriceSource
from app.modules.chat.answers import content_terms, pick_sentences
from app.modules.chat.metrics import MetricResult, get_metric, load_annual_facts
from app.modules.chat.symbols import Company, aliases_for
from app.modules.llm.trace import TraceRecorder
from app.modules.paper.context import EngineContext
from app.modules.portfolio import service as portfolio_service
from app.modules.research.retrieval import RetrievedChunk, hybrid_search

#: names of the registry tools these functions implement (asserted in tests against TOOL_REGISTRY)
#: share of question terms missing from the whole filing corpus above which the question counts as unanswerable
ABSENT_SHARE = 0.25

TOOL_NAMES = ("get_portfolio", "evaluate_risk", "get_xbrl_metric", "search_filings")


async def get_portfolio(
    conn: Connection, user_id: UUID, ctx: EngineContext, rec: TraceRecorder
) -> dict[str, Any]:
    with rec.step("Tool: get_portfolio") as st:
        pf = await portfolio_service.get_portfolio(conn, user_id, ctx)
        st.detail["summary"] = f"{len(pf['positions'])} positions"
    return pf


async def evaluate_risk(
    conn: Connection, user_id: UUID, ctx: EngineContext, rec: TraceRecorder
) -> dict[str, Any]:
    with rec.step("Tool: evaluate_risk") as st:
        risk = await portfolio_service.get_risk(conn, user_id, ctx)
        h = risk["metrics"].get("hhi")
        st.detail["summary"] = "HHI n/m" if h is None else f"HHI {h:.2f}"
        st.detail["rules_outside_limit"] = sum(1 for r in risk["rules"] if r["status"] == "breach")
    return risk


async def get_xbrl_metrics(
    conn: Connection,
    companies: tuple[Company, ...],
    metrics: tuple[str, ...],
    as_of: date,
    price_source: PriceSource | None,
    rec: TraceRecorder,
) -> list[MetricResult]:
    out: list[MetricResult] = []
    for c in companies:
        facts = await load_annual_facts(conn, c.symbol, as_of)
        for key in metrics:
            with rec.step(f"Tool: get_xbrl_metric {c.symbol} {key}") as st:
                res = await get_metric(conn, c, key, as_of, price_source, facts=facts)
                st.detail["summary"] = res.display
                st.detail["accession_no"] = res.accession_nos[0] if res.accession_nos else None
            out.append(res)
    return out


async def absent_terms(conn: Connection, symbol: str, terms: list[str]) -> list[str]:
    """Question terms that occur nowhere in the company's indexed filings (e.g. "antarctica", "dragon").

    A question built around a word the filings never use is not answered by loosely related passages."""
    out: list[str] = []
    for t in terms:
        q = re.sub(r"[^a-z0-9]", "", t)
        if not q:
            continue
        hit = await conn.fetchval(
            "select 1 from doc_chunks where symbol = $1 and not quarantined and tsv @@ to_tsquery('english', $2) limit 1",
            symbol,
            f"{q}:*",
        )
        if hit is None:
            out.append(t)
    return out


async def search_filings(
    conn: Connection,
    company: Company,
    message: str,
    items: tuple[str, ...],
    as_of: date,
    rec: TraceRecorder,
    k: int = 7,
) -> tuple[list[RetrievedChunk], list[str]]:
    """Hybrid retrieval over this company's non-quarantined chunks, point-in-time (T-RAG-06)."""
    company_tokens = {t for a in aliases_for(company) for t in a.split()} | {company.symbol.lower()}
    terms = content_terms(message, company_tokens)
    query = " ".join(terms) if terms else (message + " business overview")
    with rec.step("Tool: search_filings", symbol=company.symbol) as st:
        latest = await conn.fetchval(
            "select accession_no from raw_documents where symbol = $1 and form = '10-K' and filed_at::date <= $2 "
            "order by filed_at desc limit 1",
            company.symbol,
            as_of,
        )
        forms = ["10-K"] if latest else None
        accs = [str(latest)] if latest else None
        chunks = await hybrid_search(
            conn,
            company.symbol,
            query,
            as_of=as_of,
            items=list(items) or None,
            k=k,
            forms=forms,
            accession_nos=accs,
        )
        if not chunks and items:  # topic hint was too narrow for what was ingested
            chunks = await hybrid_search(
                conn, company.symbol, query, as_of=as_of, k=k, forms=forms, accession_nos=accs
            )
        st.detail["filing"] = latest
        st.detail["summary"] = f"{len(chunks)} chunks"
        st.detail["as_of"] = as_of.isoformat()
        absent = await absent_terms(conn, company.symbol, terms)
        if absent and len(absent) / len(terms) >= ABSENT_SHARE:
            st.detail["absent_terms"] = len(absent)
            chunks = []  # the question is about something the filings never mention
    return chunks, terms


def sentences_for(chunks: list[RetrievedChunk], terms: list[str]) -> list[tuple[RetrievedChunk, str, int]]:
    return pick_sentences(chunks, terms)
