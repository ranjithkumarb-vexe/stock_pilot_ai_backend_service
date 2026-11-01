"""`POST /v1/events`: batched client telemetry (SPEC 10.2). Max 100 per batch, per-event validation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import Field

from app.core.audit import write_audit
from app.core.clock import Clock
from app.core.deps import ClockDep, CurrentUser, UserConn
from app.core.errors import AppError, ErrorCode, validation_error
from app.core.ratelimit import TokenBucketLimiter
from app.core.schemas import ApiModel, ResponseModel
from app.modules.events.validation import MAX_BATCH, Rejected, ValidEvent, validate_event

router = APIRouter(tags=["events"])

EVENTS_BURST = 300  # events a user may send at once
EVENTS_PER_S = 20.0  # sustained rate


class EventsRequest(ApiModel):
    events: list[Any] = Field(
        description="Up to 100 events: {event_id, ts, session_id, type, entity, props, context, client, schema_version}"
    )


class RejectedEvent(ResponseModel):
    index: int
    reason: str


class EventsResponse(ResponseModel):
    accepted: int
    rejected: list[RejectedEvent]


def _limiter(request: Request, clock: Clock) -> TokenBucketLimiter:
    limiter: TokenBucketLimiter | None = getattr(request.app.state, "events_limiter", None)
    if limiter is None:
        limiter = TokenBucketLimiter(EVENTS_BURST, EVENTS_PER_S, clock.monotonic)
        request.app.state.events_limiter = limiter
    return limiter


_INSERT = """
insert into events (event_id, user_id, ts, session_id, type, entity, props, context, client, schema_version)
values ($1, $2, $3, $4, $5, $6, $7, $8, $9, 1)
on conflict (user_id, event_id) do nothing
"""


@router.post("/events", response_model=EventsResponse, summary="Ingest a batch of client events")
async def post_events(
    body: EventsRequest, user: CurrentUser, conn: UserConn, clock: ClockDep, request: Request
) -> EventsResponse:
    if len(body.events) > MAX_BATCH:
        raise validation_error(
            f"At most {MAX_BATCH} events per batch", {"max": MAX_BATCH, "got": len(body.events)}
        )
    now = clock.now()
    valid: list[ValidEvent] = []
    rejected: list[RejectedEvent] = []
    seen: set[Any] = set()
    for index, raw in enumerate(body.events):
        try:
            ev = validate_event(raw, now)
        except Rejected as exc:
            rejected.append(RejectedEvent(index=index, reason=exc.reason))
            continue
        if ev.event_id in seen:  # duplicate inside the batch: keep the first
            rejected.append(RejectedEvent(index=index, reason="duplicate_event_id"))
            continue
        seen.add(ev.event_id)
        valid.append(ev)

    if valid:  # only events that will be stored consume rate-limit budget
        wait = _limiter(request, clock).check(f"events:{user.user_id}", cost=float(len(valid)))
        if wait > 0:
            retry = max(1, int(wait) + 1)
            raise AppError(
                ErrorCode.RATE_LIMITED,
                "Too many events",
                details={"retry_after_s": retry},
                headers={"Retry-After": str(retry)},
            )
        await conn.executemany(
            _INSERT,
            [
                (e.event_id, user.user_id, e.ts, e.session_id, e.type, e.entity, e.props, e.context, e.client)
                for e in valid
            ],
        )
        scheduler = getattr(request.app.state, "feature_scheduler", None)
        if scheduler is not None:
            scheduler.mark_dirty(user.user_id)
    await write_audit(
        conn,
        user_id=user.user_id,
        action="events.ingest",
        entity="events",
        details={"accepted": len(valid), "rejected": len(rejected)},
    )
    return EventsResponse(accepted=len(valid), rejected=rejected)
