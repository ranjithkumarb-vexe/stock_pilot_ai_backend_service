# ruff: noqa: S608  (only module-level constants are interpolated into SQL)
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.errors import not_found
from app.modules.audit.naming import display_name, kind_of, mode_label, step_detail, step_outcome
from app.modules.audit.schemas import StepOut, TraceOut

_STEPS_SQL = "select trace_ref, idx, name, ms, outcome, detail from trace_steps where trace_ref = any($1::uuid[]) order by trace_ref, idx"
_COLS = "id, trace_id, user_id, name, model, mode, duration_ms, tokens_in, tokens_out, cost_usd, outcome, created_at"


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _steps_by_trace(rows: list[Any]) -> dict[Any, list[StepOut]]:
    out: dict[Any, list[StepOut]] = {}
    for r in rows:
        out.setdefault(r["trace_ref"], []).append(
            StepOut(
                name=r["name"],
                ms=int(r["ms"] or 0),
                outcome=step_outcome(r["outcome"]),
                detail=step_detail(r["detail"]),
            )
        )
    return out


def to_out(row: Any, steps: list[StepOut]) -> TraceOut:
    name: str = row["name"]
    total_ms = max(int(row["duration_ms"] or 0), sum(s.ms for s in steps))
    return TraceOut(
        id=str(row["id"]), trace_id=row["trace_id"], name=display_name(name),
        model=row["model"] or "n/a", mode=mode_label(row["mode"], row["model"], row["outcome"], name),
        duration_ms=total_ms, tokens_in=int(row["tokens_in"] or 0), tokens_out=int(row["tokens_out"] or 0),
        cost_usd=float(row["cost_usd"] or 0), outcome=row["outcome"], kind=kind_of(name),
        scope="user" if row["user_id"] is not None else "system", created_at=iso(row["created_at"]), steps=steps,
    )  # fmt: skip


async def list_traces(
    conn: Connection, limit: int, cursor: str | None, outcome: str | None, kind: str | None
) -> tuple[list[TraceOut], str | None]:
    before: datetime | None = None
    if cursor:
        before = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    prefix = None if kind is None else f"{kind}:%"
    args = (before, outcome, prefix, limit + 1)
    where = (
        "($1::timestamptz is null or created_at < $1) and ($2::text is null or outcome = $2) "
        "and ($3::text is null or name like $3)"
    )
    mine = await conn.fetch(
        f"select {_COLS} from traces where {where} order by created_at desc limit $4", *args
    )
    system = await conn.fetch(
        f"select {_COLS} from audit_system_traces(200) where {where} order by created_at desc limit $4",
        *args,
    )
    rows = sorted([*mine, *system], key=lambda r: (r["created_at"], str(r["id"])), reverse=True)
    more = len(rows) > limit
    rows = rows[:limit]
    steps: dict[Any, list[StepOut]] = {}
    my_ids = [r["id"] for r in rows if r["user_id"] is not None]
    if my_ids:
        step_rows = await conn.fetch(_STEPS_SQL, my_ids)
        steps.update(_steps_by_trace(list(step_rows)))
    for r in rows:
        if r["user_id"] is None:
            sys_steps = await conn.fetch("select trace_ref, idx, name, ms, outcome, detail from audit_system_trace_steps($1)", r["id"])  # fmt: skip
            steps.update(_steps_by_trace(list(sys_steps)))
    items = [to_out(r, steps.get(r["id"], [])) for r in rows]
    return items, (iso(rows[-1]["created_at"]) if more and rows else None)


async def get_trace(conn: Connection, ident: str) -> TraceOut:
    """By `traces.id` (uuid) or by request trace id (`X-Trace-Id`, as returned in chat/brief responses)."""
    row = None
    try:
        uid: UUID | None = UUID(ident)
    except ValueError:
        uid = None
    if uid is not None:
        row = await conn.fetchrow(f"select {_COLS} from traces where id = $1", uid)
        if row is None:
            row = await conn.fetchrow(f"select {_COLS} from audit_system_traces(200) where id = $1", uid)
    if row is None:  # a request trace id is 32 hex characters, which also parses as a UUID
        row = await conn.fetchrow(
            f"select {_COLS} from traces where trace_id = $1 order by created_at desc limit 1", ident
        )
    if row is None:
        row = await conn.fetchrow(
            f"select {_COLS} from audit_system_traces(200) where trace_id = $1 limit 1", ident
        )
    if row is None:
        raise not_found("Trace not found")
    if row["user_id"] is not None:
        srows = await conn.fetch("select trace_ref, idx, name, ms, outcome, detail from trace_steps where trace_ref = $1 order by idx", row["id"])  # fmt: skip
    else:
        srows = await conn.fetch("select trace_ref, idx, name, ms, outcome, detail from audit_system_trace_steps($1)", row["id"])  # fmt: skip
    return to_out(row, _steps_by_trace(list(srows)).get(row["id"], []))
