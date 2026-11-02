"""`POST /v1/chat` and `GET /v1/chat/history` (CONTRACT: Chat)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.core.audit import write_audit
from app.core.deps import ClockDep, CurrentUser, ExpensiveCall, UserConn
from app.core.logging import trace_id_var
from app.core.pricesource import PriceSource
from app.modules.chat import service, store
from app.modules.chat.schemas import ChatRequest, ChatResponse, HistoryOut
from app.modules.llm.deps import LlmDep
from app.modules.llm.trace import TraceRecorder, use_trace
from app.modules.paper.context import EngineCtx

router = APIRouter(tags=["chat"])

TITLES = {
    "portfolio_concentration": "Portfolio concentration",
    "portfolio_risk": "Portfolio risk and rules",
    "portfolio_overview": "Portfolio overview",
    "filing_metric": "Reported figures",
    "filing_text": "Filing disclosures",
    "glossary": "Definitions",
    "app_help": "Using StockPilot",
    "unknown": "Question",
    "refused": "Question",
}


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Ask a question about your portfolio, rules or ingested filings",
    dependencies=[ExpensiveCall],
)
async def post_chat(
    body: ChatRequest,
    request: Request,
    user: CurrentUser,
    conn: UserConn,
    clock: ClockDep,
    llm: LlmDep,
    ctx: EngineCtx,
) -> ChatResponse:
    now = clock.now()
    thread_id = await store.ensure_thread(conn, user.user_id, body.thread_id, None)
    await store.purge_expired(conn, user.user_id, now)
    price_source: PriceSource | None = getattr(request.app.state, "price_source", None)
    rec = TraceRecorder("chat:pending", user_id=user.user_id, clock=clock)
    try:
        with use_trace(rec):
            result = await service.respond(
                conn, body.message, rec, llm,
                user_id=user.user_id, ctx=ctx, price_source=price_source, as_of=now.date(),
            )  # fmt: skip
    except BaseException:
        rec.set_outcome("error")
        await rec.flush()
        raise
    await rec.flush()
    trace_id = rec.trace_id or trace_id_var.get()
    await store.add_message(
        conn, user.user_id, thread_id, role="user", content=body.message, trace_id=trace_id
    )
    msg_id, _ = await store.add_message(
        conn, user.user_id, thread_id, role="assistant", content=result.answer, citations=result.citations,
        refused=result.refused, refusal_reason=result.refusal_reason, trace_id=trace_id,
        intent=result.intent, ledger=result.ledger.as_json(),
    )  # fmt: skip
    if body.thread_id is None:
        await conn.execute(
            "update chat_threads set title = $3 where id = $1 and user_id = $2",
            thread_id, user.user_id, TITLES.get(result.intent, "Question"),
        )  # fmt: skip
    await write_audit(
        conn, user_id=user.user_id, action="chat.message", entity="chat_message", entity_id=str(msg_id),
        details={"intent": result.intent, "refused": result.refused, "grounded": result.grounded},
    )  # fmt: skip
    return ChatResponse(
        id=str(msg_id), thread_id=str(thread_id), answer=result.answer, citations=result.citations,
        refused=result.refused, refusal_reason=result.refusal_reason, trace_id=trace_id,
        intent=result.intent, grounded=result.grounded,
    )  # fmt: skip


@router.get("/chat/history", response_model=HistoryOut, summary="Messages of a thread (default: the latest)")
async def get_history(
    user: CurrentUser,
    conn: UserConn,
    thread_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> HistoryOut:
    items, nxt = await store.history(conn, user.user_id, thread_id, limit, cursor)
    return HistoryOut(items=items, next_cursor=nxt)
