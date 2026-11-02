"""Research endpoints: `GET /v1/briefs/{symbol}`, `POST /v1/briefs/{symbol}/generate`, `GET /v1/citations/{id}`."""

from __future__ import annotations

import re
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Request

from app.core.audit import write_audit
from app.core.deps import ClockDep, CurrentUser, ExpensiveCall, UserConn
from app.core.errors import AppError, ErrorCode, not_found
from app.core.pricesource import PriceSource
from app.modules.llm.deps import LlmDep
from app.modules.llm.trace import TraceRecorder, use_trace
from app.modules.research.brief import templates  # noqa: F401  (registers the mock "brief_claims" handler)
from app.modules.research.brief.generate import generate_brief
from app.modules.research.brief.schemas import BriefOut, CitationOut
from app.modules.research.brief.store import (
    citation_detail,
    find_cached,
    lock_brief_key,
    save_brief,
    to_brief_out,
)
from app.modules.research.brief.style import load_style_card

router = APIRouter(tags=["research"])
_SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,10}$")


async def _check_symbol(conn: UserConn, raw: str) -> str:
    symbol = raw.upper()
    if not _SYMBOL.match(symbol) or not await conn.fetchval(
        "select 1 from tickers where symbol = $1 and in_universe", symbol
    ):
        raise AppError(ErrorCode.UNSUPPORTED_SYMBOL, f"{raw[:10]} is not in the supported universe")
    return symbol


async def _build_and_store(
    request: Request,
    conn: UserConn,
    user: CurrentUser,
    clock: ClockDep,
    llm: LlmDep,
    symbol: str,
    as_of: date,
) -> BriefOut:
    style = await load_style_card(conn, user.user_id)
    await lock_brief_key(conn, user.user_id, symbol, as_of, style.style_hash())
    price_source: PriceSource | None = getattr(request.app.state, "price_source", None)
    rec = TraceRecorder(f"brief:{symbol}", user_id=user.user_id, clock=clock)
    with use_trace(rec):
        built = await generate_brief(
            conn, user_id=user.user_id, symbol=symbol, as_of=as_of, style=style, llm=llm,
            price_source=price_source, recorder=rec,
        )  # fmt: skip
    rec.set_outcome("ok" if built.status == "ok" else "error")
    await rec.flush()
    row = await save_brief(conn, user.user_id, built, rec.trace_id)
    return to_brief_out(row)


@router.get(
    "/briefs/{symbol}", response_model=BriefOut, summary="Research brief (cached per symbol, day and style)"
)
async def get_brief(
    symbol: str, request: Request, user: CurrentUser, conn: UserConn, clock: ClockDep, llm: LlmDep
) -> BriefOut:
    sym = await _check_symbol(conn, symbol)
    as_of = clock.now().date()
    style = await load_style_card(conn, user.user_id)
    await lock_brief_key(conn, user.user_id, sym, as_of, style.style_hash())
    cached = await find_cached(conn, user.user_id, sym, as_of, style.style_hash())
    if cached is not None:
        return to_brief_out(cached)
    return await _build_and_store(request, conn, user, clock, llm, sym, as_of)


@router.post(
    "/briefs/{symbol}/generate",
    response_model=BriefOut,
    summary="Regenerate the brief now",
    dependencies=[ExpensiveCall],
)
async def regenerate_brief(
    symbol: str, request: Request, user: CurrentUser, conn: UserConn, clock: ClockDep, llm: LlmDep
) -> BriefOut:
    sym = await _check_symbol(conn, symbol)
    out = await _build_and_store(request, conn, user, clock, llm, sym, clock.now().date())
    await write_audit(
        conn, user_id=user.user_id, action="brief.generate", entity="brief", entity_id=out.id,
        details={"symbol": sym, "status": out.status},
    )  # fmt: skip
    return out


@router.get("/citations/{citation_id}", response_model=CitationOut, summary="Source behind a citation")
async def get_citation(citation_id: UUID, user: CurrentUser, conn: UserConn) -> CitationOut:
    detail = await citation_detail(conn, citation_id)
    if detail is None:
        raise not_found("Citation not found")
    return detail
