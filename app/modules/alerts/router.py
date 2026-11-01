"""Alerts API: list, act on, and refresh (BE-17). The lifespan owns the in-process monitor (APScheduler)."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, Query, Request

from app.core.db import user_tx
from app.core.deps import ClockDep, CurrentUser, SettingsDep, UserConn
from app.core.errors import validation_error
from app.core.logging import get_logger
from app.modules.alerts import service
from app.modules.alerts.earnings import FilingHistoryEarnings
from app.modules.alerts.filings import EdgarSubmissionsSource, FilingSource, RawDocumentsSource, load_ciks
from app.modules.alerts.monitor import AlertMonitor, env_flag, interval_from_env
from app.modules.alerts.schemas import (
    AlertActionIn,
    AlertOut,
    AlertsPage,
    AlertType,
    RefreshOut,
)
from app.modules.alerts.store import unsnooze_due
from app.modules.alerts.view import alert_view
from app.modules.edgar.client import EdgarClient
from app.modules.ranking import why

log = get_logger("app.alerts.router")
router = APIRouter(tags=["alerts"])

STATE_ALIASES = {"read": "opened"}
MAX_PAGE = 200


def get_monitor(request: Request) -> AlertMonitor:
    mon: AlertMonitor | None = getattr(request.app.state, "alert_monitor", None)
    if mon is None:  # app built without the lifespan (should not happen in the service)
        mon = AlertMonitor(
            request.app.state, sources=[RawDocumentsSource()], earnings=FilingHistoryEarnings()
        )
        request.app.state.alert_monitor = mon
    return mon


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    sources: list[FilingSource] = [RawDocumentsSource()]
    client: EdgarClient | None = None
    if env_flag("ALERTS_EDGAR_LIVE", True):
        try:
            client = EdgarClient(app.state.settings.sec_user_agent, clock=app.state.clock)
            sources.append(EdgarSubmissionsSource(client, load_ciks, max_age_s=60.0))
        except ValueError:  # placeholder User-Agent: stay on the ingested filings only (fair-access policy)
            log.warning("SEC_USER_AGENT is not set to a real contact: live 8-K polling is off")
    monitor = AlertMonitor(
        app.state, sources=sources, earnings=FilingHistoryEarnings(), interval_s=interval_from_env()
    )
    app.state.alert_monitor = monitor
    if env_flag("ALERTS_AUTOSTART", True):
        monitor.start()
    try:
        yield
    finally:
        await monitor.stop()
        if client is not None:
            await client.aclose()


def _encode_cursor(ts: datetime, alert_id: str) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{alert_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, _, aid = raw.partition("|")
        return datetime.fromisoformat(ts), UUID(aid)
    except (ValueError, UnicodeDecodeError) as exc:
        raise validation_error("Invalid cursor") from exc


async def _with_why(conn: Any, user_id: UUID, views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not views:
        return views
    rows = {
        r["output_id"]: r
        for r in await conn.fetch(
            "select distinct on (output_id) * from personalization_audit "
            "where user_id = $1 and output_type = 'alert' and output_id = any($2::text[]) "
            "order by output_id, created_at desc, id",
            user_id,
            [v["id"] for v in views],
        )
    }
    for v in views:
        row = rows.get(v["id"])
        if row is not None:
            v["why"] = why.build_why(row)
        elif v["critical"]:
            v["why"] = {
                "summary": why.SUMMARY_PINNED,
                "top_features": [],
                "rules_checked": [{"rule": "Pinned by the risk engine", "passed": True}],
                "sources": [v["source_label"]],
                "basis": "profile",
            }
    return views


@router.get("/alerts", response_model=AlertsPage, summary="My alerts (newest first)")
async def list_alerts(
    user: CurrentUser,
    conn: UserConn,
    clock: ClockDep,
    type: Annotated[AlertType | None, Query()] = None,
    state: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> AlertsPage:
    now = clock.now()
    await unsnooze_due(conn, user.user_id, now)
    states: list[str] | None = None
    if state:
        name = STATE_ALIASES.get(state, state)
        if name == "archived":
            states = ["dismissed", "snoozed"]
        elif name in ("new", "opened", "dismissed", "snoozed"):
            states = [name]
        else:
            raise validation_error("state must be one of new, opened (read), dismissed, snoozed, archived")
    args: list[Any] = [user.user_id]
    where = ["user_id = $1"]
    if type:
        args.append(type)
        where.append(f"type = ${len(args)}")
    if states:
        args.append(states)
        where.append(f"state = any(${len(args)}::text[])")
    if cursor:
        ts, aid = _decode_cursor(cursor)
        args += [ts, aid]
        where.append(f"(created_at, id) < (${len(args) - 1}, ${len(args)})")
    args.append(limit + 1)
    sql = (  # only whitelisted fragments are concatenated; every value is a bound parameter
        "select * from alerts where "  # noqa: S608
        + " and ".join(where)
        + f" order by created_at desc, id desc limit ${len(args)}"
    )
    rows = await conn.fetch(sql, *args)
    more = len(rows) > limit
    rows = rows[:limit]
    views = await _with_why(conn, user.user_id, [alert_view(r) for r in rows])
    thr = await service.load_threshold(conn, user.user_id)
    return AlertsPage.model_validate(
        {
            "items": views,
            "next_cursor": _encode_cursor(rows[-1]["created_at"], str(rows[-1]["id"]))
            if more and rows
            else None,
            "move_threshold": thr["k"],
            "fatigue_index": thr["fatigue"],
            "min_materiality": thr["min_materiality"],
        }
    )


@router.post("/alerts/refresh", response_model=RefreshOut, summary="Evaluate my alerts now (on demand)")
async def refresh_alerts(
    user: CurrentUser, request: Request, clock: ClockDep, settings: SettingsDep
) -> RefreshOut:
    mon = get_monitor(request)
    now = clock.now()
    async with user_tx(user.user_id) as conn:
        syms = {
            r["symbol"]
            for r in await conn.fetch(
                "select symbol from positions where user_id = $1 and qty > 0", user.user_id
            )
        }
        syms |= {
            r["symbol"]
            for r in await conn.fetch("select symbol from watchlist_items where user_id = $1", user.user_id)
        }
    signals = await mon.collect(sorted(syms), now)
    ec = mon.eval_context(now, signals)
    async with user_tx(user.user_id) as conn:
        res = await service.evaluate_user(conn, user.user_id, ec)
        views = await _with_why(conn, user.user_id, list(res.created))
    for v in res.created:  # after the commit
        mon.deliver(user.user_id, v)
    return RefreshOut.model_validate(
        {"created": len(views), "resolved": res.resolved, "items": views, "move_threshold": res.k}
    )


@router.post(
    "/alerts/{alert_id}/action", response_model=AlertOut, summary="Open, dismiss, snooze 24 h or restore"
)
async def alert_action(
    alert_id: UUID, body: AlertActionIn, user: CurrentUser, conn: UserConn, clock: ClockDep
) -> AlertOut:
    view = await service.apply_action(conn, user.user_id, alert_id, body.action, clock.now())
    (view,) = await _with_why(conn, user.user_id, [view])
    return AlertOut.model_validate(view)
