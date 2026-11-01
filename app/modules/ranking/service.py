"""Feed orchestration (BE-16): candidates -> filters -> score -> MMR -> pinned -> audit -> `GET /feed` items,
and `GET /why/*` built from the audit rows."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.errors import not_found
from app.core.logging import get_logger
from app.modules.paper.context import EngineContext
from app.modules.ranking import bandit, candidates, why
from app.modules.ranking.briefing import briefing_id
from app.modules.ranking.models import ALERT_TYPE_ARM, Arm, Candidate, Scored
from app.modules.ranking.scoring import rank

log = get_logger("app.ranking.service")

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
RNG_BUCKET_S = (
    300  # the Thompson draw is stable inside a 5-minute window so the feed does not jitter on reload
)
OUTPUT_TYPES = ("alert", "proposal", "briefing", "brief")
FEEDBACK_WATERMARK = "feed_feedback_watermark"


@dataclass(slots=True)
class FeedResult:
    items: list[dict[str, Any]]
    total: int
    next_cursor: str | None
    dropped: list[tuple[str, str]] = field(default_factory=list)


def rng_for(user_id: UUID, now: datetime, salt: str = "") -> random.Random:
    return random.Random(f"{user_id}:{int(now.timestamp() // RNG_BUCKET_S)}:{salt}")  # noqa: S311 - ranking draw, not security


async def load_interests(conn: Connection, user_id: UUID) -> dict[str, float]:
    rows = await conn.fetch("select dim, weight from user_interest_vector where user_id = $1", user_id)
    return {r["dim"]: float(r["weight"]) for r in rows}


# ------------------------------------------------------------------ feedback -> bandit
async def sync_feedback(conn: Connection, user_id: UUID, now: datetime) -> int:
    """Apply `thumbs_up|down` events sent from the feed cards (`props.action` starts with `feed_`) to the bandit.
    A watermark in `user_features` makes it idempotent; the per-user limiter still applies."""
    mark = await conn.fetchval(
        "select value ->> 'at' from user_features where user_id = $1 and key = $2",
        user_id,
        FEEDBACK_WATERMARK,
    )
    since = datetime.fromisoformat(mark) if mark else datetime(1970, 1, 1, tzinfo=UTC)
    rows = await conn.fetch(
        "select type, entity, props, received_at from events where user_id = $1 and received_at > $2 "
        "and type in ('thumbs_up', 'thumbs_down') order by received_at limit 50",
        user_id,
        since,
    )
    if not rows:
        return 0
    applied = 0
    for r in rows:
        props, entity = r["props"] or {}, r["entity"] or {}
        action = str(props.get("action") or "")
        if not action.startswith("feed_"):
            continue
        reward = bandit.REWARD_USEFUL if r["type"] == "thumbs_up" else bandit.REWARD_NOT_USEFUL
        arm, symbol = await _arm_of_feedback(conn, user_id, action, entity)
        if arm is None:
            continue
        sector = await bandit.sector_of(conn, symbol)
        applied += await bandit.apply_reward(conn, user_id, arm, reward, sector=sector, now=now)
    await conn.execute(
        "insert into user_features (user_id, key, value, confidence, updated_at) values ($1, $2, $3, 1, $4) "
        "on conflict (user_id, key) do update set value = excluded.value, updated_at = excluded.updated_at",
        user_id, FEEDBACK_WATERMARK, {"at": rows[-1]["received_at"].isoformat()}, now,
    )  # fmt: skip
    return applied


async def _arm_of_feedback(
    conn: Connection, user_id: UUID, action: str, entity: dict[str, Any]
) -> tuple[str | None, str | None]:
    if action == "feed_briefing":
        return "brief", None
    if action == "feed_alert" and entity.get("alert_id"):
        try:
            row = await conn.fetchrow(
                "select type, symbol from alerts where user_id = $1 and id = $2",
                user_id,
                UUID(str(entity["alert_id"])),
            )
        except ValueError:
            return None, None
        return (None, None) if row is None else (ALERT_TYPE_ARM.get(row["type"], "brief"), row["symbol"])
    if action == "feed_proposal" and entity.get("proposal_id"):
        try:
            row = await conn.fetchrow(
                "select symbol from proposals where user_id = $1 and id = $2",
                user_id,
                UUID(str(entity["proposal_id"])),
            )
        except ValueError:
            return None, None
        return (None, None) if row is None else ("proposal", row["symbol"])
    return None, None


# ------------------------------------------------------------------ feed
def _basis_for(ranked: list[Scored], priors: dict[str, Arm], n_events: int) -> dict[str, why.Basis]:
    out: dict[str, why.Basis] = {}
    for s in ranked:
        prior = priors.get(s.cand.arm)
        shift = abs(s.arm_mean - prior.mean) if prior is not None else 0.0
        out[s.cand.id] = why.determine_basis(s.pinned, n_events, shift)
    return out


async def rank_candidates(
    conn: Connection,
    user_id: UUID,
    ctx: EngineContext,
    now: datetime,
    *,
    rng: random.Random | None = None,
    page_size: int | None = None,
    write_audit_rows: bool = True,
    audit_limit: int | None = None,
) -> tuple[list[Scored], list[tuple[Candidate, str]], dict[str, why.Basis]]:
    """The whole ranking pipeline for one user; returns the full ordered list (not just the page)."""
    await sync_feedback(conn, user_id, now)
    alerts = await candidates.alert_candidates(conn, user_id, now)
    proposals = await candidates.proposal_candidates(conn, user_id, now)
    top = max((a for a in alerts if not a.critical), key=lambda a: a.materiality, default=None)
    briefing = await candidates.briefing_candidates(
        conn, user_id, ctx, now, top_item_title=top.title if top else None, n_pending=len(proposals)
    )
    kept, dropped = await candidates.apply_filters(conn, user_id, ctx, [*alerts, *proposals, *briefing])
    arms = await bandit.load_arms(conn, user_id)
    priors = await bandit.load_priors(conn, user_id)
    interests = await load_interests(conn, user_id)
    ranked = rank(kept, arms, interests, rng or rng_for(user_id, now), page_size=page_size, priors=priors)
    n_events = await why.behaviour_event_count(conn, user_id, now)
    basis = _basis_for(ranked, priors, n_events)
    if write_audit_rows:
        await why.write_rows(conn, user_id, ranked[:audit_limit] if audit_limit else ranked, basis, now)
    return ranked, dropped, basis


async def build_feed(
    conn: Connection,
    user_id: UUID,
    ctx: EngineContext,
    now: datetime,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    rng: random.Random | None = None,
) -> FeedResult:
    limit = max(1, min(MAX_LIMIT, limit))
    ranked, dropped, _ = await rank_candidates(
        conn, user_id, ctx, now, rng=rng, page_size=limit, audit_limit=offset + limit
    )
    page = ranked[offset : offset + limit]
    rows = {
        (r["output_type"], r["output_id"]): r
        for r in await conn.fetch(
            "select distinct on (output_type, output_id) * from personalization_audit "
            "where user_id = $1 and output_id = any($2::text[]) order by output_type, output_id, created_at desc, id",
            user_id,
            [s.cand.id for s in page],
        )
    }
    items: list[dict[str, Any]] = []
    for s in page:
        row = rows.get((s.cand.kind, s.cand.id))
        items.append(
            {
                "kind": s.cand.kind,
                "id": s.cand.id,
                "pinned": s.pinned,
                "score": round(s.score, 4),
                "item": s.cand.item,
                "why": why.build_why(row) if row is not None else _fallback_why(s),
            }
        )
    nxt = str(offset + limit) if offset + limit < len(ranked) else None
    return FeedResult(items, len(ranked), nxt, [(c.id, r) for c, r in dropped])


def _fallback_why(s: Scored) -> dict[str, Any]:
    return {
        "summary": why.SUMMARY_PINNED if s.pinned else why.SUMMARY_PROFILE,
        "top_features": [],
        "rules_checked": [],
        "sources": s.cand.sources,
        "basis": "profile",
    }


# ------------------------------------------------------------------ why
async def explain(
    conn: Connection, user_id: UUID, ctx: EngineContext, now: datetime, output_type: str, output_id: str
) -> dict[str, Any]:
    """`GET /why/{output_type}/{id}`: from the stored audit row; if the item was never ranked it is ranked now
    (which writes the row). Unknown / foreign ids are 404 (RLS hides other users' rows)."""
    if output_type not in OUTPUT_TYPES:
        raise not_found("Unknown output type")
    if output_type == "brief":
        return await _explain_brief(conn, user_id, now, output_id)
    row = await why.latest_row(conn, user_id, output_type, output_id)
    if row is None:
        await _rank_single(conn, user_id, ctx, now, output_type, output_id)
        row = await why.latest_row(conn, user_id, output_type, output_id)
    if row is None:
        raise not_found("Nothing to explain for this id")
    return why.build_why(row)


async def _rank_single(
    conn: Connection, user_id: UUID, ctx: EngineContext, now: datetime, output_type: str, output_id: str
) -> None:
    # 1) it may simply be on the live feed
    ranked, _, _basis = await rank_candidates(conn, user_id, ctx, now, page_size=MAX_LIMIT)
    if any(s.cand.kind == output_type and s.cand.id == output_id for s in ranked):
        return
    # 2) a dismissed / decided / expired item can still be explained: score it on its own
    cand: Candidate | None = None
    try:
        uid = UUID(output_id)
    except ValueError:
        return
    if output_type == "alert":
        cand = await candidates.alert_candidate_by_id(conn, user_id, uid)
    elif output_type == "proposal":
        cand = await candidates.proposal_candidate_by_id(conn, user_id, uid)
    elif output_type == "briefing" and uid == briefing_id(user_id, now.date()):
        return
    if cand is None:
        return
    arms = await bandit.load_arms(conn, user_id)
    priors = await bandit.load_priors(conn, user_id)
    interests = await load_interests(conn, user_id)
    one = rank([cand], arms, interests, rng_for(user_id, now), priors=priors)
    n_events = await why.behaviour_event_count(conn, user_id, now)
    await why.write_rows(conn, user_id, one, _basis_for(one, priors, n_events), now)


async def _explain_brief(conn: Connection, user_id: UUID, now: datetime, output_id: str) -> dict[str, Any]:
    try:
        bid = UUID(output_id)
    except ValueError as exc:
        raise not_found("Brief not found") from exc
    brief = await conn.fetchrow("select id, symbol from briefs where user_id = $1 and id = $2", user_id, bid)
    if brief is None:
        raise not_found("Brief not found")
    card = await conn.fetchval(
        "select value from user_features where user_id = $1 and key = 'style_card'", user_id
    )
    card = card if isinstance(card, dict) else {}
    n_events = await why.behaviour_event_count(conn, user_id, now)
    basis: why.Basis = "behaviour" if n_events >= why.BEHAVIOUR_MIN_EVENTS else "cold_start"
    order = [str(x) for x in (card.get("order") or card.get("section_order") or [])][:3]
    feats: list[dict[str, Any]] = []
    if order:
        feats.append(
            {"name": "section_order", "weight": 0.5, "detail": f"Sections lead with: {', '.join(order)}."}
        )
    if card.get("depth"):
        feats.append({"name": "depth", "weight": 0.3, "detail": f"Depth set to {card['depth']}."})
    if card.get("jargon"):
        feats.append(
            {"name": "jargon_level", "weight": 0.2, "detail": f"Jargon level {card['jargon']} of 3."}
        )
    summary = (
        "Based on your setup: the section order and depth start from your answers."
        if basis == "cold_start"
        else "Section order and depth follow your style card, learned from how you read past briefs."
    )
    payload = {
        "summary": summary,
        "top_features": feats[:3],
        "rules_checked": [{"rule": "Facts and figures are identical for every user", "passed": True}],
        "sources": ["Your style card"],
        "basis": basis,
    }
    recent = await conn.fetchval(
        "select 1 from personalization_audit where user_id = $1 and output_type = 'brief' and output_id = $2 "
        "and created_at > $3 limit 1",
        user_id, output_id, now - why.REUSE_WINDOW,
    )  # fmt: skip
    if not recent:
        await conn.execute(
            "insert into personalization_audit (user_id, output_type, output_id, basis, features_used, arm_scores, "
            "rules_checked, created_at) values ($1, 'brief', $2, $3, $4, '{}'::jsonb, $5, $6)",
            user_id, output_id, basis, {"style_card": card, "symbol": brief["symbol"], "sources": payload["sources"]},
            payload["rules_checked"], now,
        )  # fmt: skip
    return payload
