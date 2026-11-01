"""Functions stream A's `/profile` aggregator (and the dev/demo tools) call. Take the caller's RLS connection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.modules.personalization.bias import SLUGS
from app.modules.personalization.features import SECTION_TITLES
from app.modules.personalization.memory import REASON_LABEL
from app.modules.personalization.recompute import recompute_features

REFRESH_AFTER = timedelta(minutes=10)
MEMORY_LIMIT = 20
EVENT_LIMIT = 10


def describe_event(etype: str, entity: dict[str, Any], props: dict[str, Any]) -> str:
    """Human line for the profile's 'recent events' list. Built from enums and identifiers only."""
    t = entity.get("ticker")
    tk = f" {t}" if isinstance(t, str) else ""
    section = SECTION_TITLES.get(str(entity.get("section")), "a section")
    reason = REASON_LABEL.get(str(props.get("reason")))
    suffix = f" (reason: {reason})" if reason else ""
    match etype:
        case "ticker_viewed":
            return f"Viewed{tk}"
        case "brief_opened":
            return f"Opened the{tk} brief"
        case "section_expanded":
            return f"Expanded {section}" + (f" on {t}" if t else "")
        case "section_dwell":
            ms = props.get("dwell_ms")
            return (
                f"Spent {round(ms / 1000)} s on {section}"
                if isinstance(ms, int | float)
                else f"Read {section}"
            )
        case "metric_clicked":
            return f"Looked up {str(entity.get('metric', 'a metric')).replace('_', ' ')}"
        case "glossary_clicked":
            return "Opened a glossary term"
        case "chat_query":
            topics = props.get("topics") or []
            return "Asked a question" + (
                f" about {', '.join(str(x).replace('_', ' ') for x in topics[:3])}" if topics else ""
            )
        case "alert_opened" | "alert_dismissed" | "alert_snoozed":
            verb = {"alert_opened": "Opened", "alert_dismissed": "Dismissed", "alert_snoozed": "Snoozed"}[
                etype
            ]
            return f"{verb} an alert" + (f" for {t}" if t else "")
        case "proposal_approved" | "proposal_rejected" | "proposal_modified":
            verb = etype.split("_")[1].capitalize()
            return f"{verb} a proposal" + (f" for {t}" if t else "") + suffix
        case "order_placed" | "order_filled":
            side = props.get("side")
            qty = props.get("qty")
            what = "Placed" if etype == "order_placed" else "Filled"
            return f"{what} {side or ''} order{tk}".replace("  ", " ") + (f" x{qty}" if qty else "")
        case "rule_edited" | "rule_override":
            return "Edited a risk rule" if etype == "rule_edited" else "Overrode a risk rule"
        case "thumbs_up" | "thumbs_down":
            return "Rated a section helpful" if etype == "thumbs_up" else "Rated a section not helpful"
        case "session_started":
            return "Started a session"
        case "onboarding_complete":
            return "Completed onboarding"
        case _:
            return etype.replace("_", " ").capitalize()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


async def get_profile_learned(
    conn: Connection, user_id: UUID, *, now: datetime | None = None, ensure_fresh: bool = True
) -> dict[str, Any]:
    """Everything the profile screen shows that was *learned* (CONTRACT /profile):

    `{interests, memories, bias, revealed_tier, gap, gap_days, gap_flag, style_card, pinned_metrics,
      recent_events}`. With `ensure_fresh` (default) the features are recomputed first when none exist yet or
    they are older than 10 minutes, so the screen is never stale and a new user gets real (cold-start) values.
    """
    now = now or datetime.now(UTC)
    rows = await conn.fetch("select key, value from user_features where user_id = $1", user_id)
    feats: dict[str, Any] = {r["key"]: r["value"] for r in rows}
    stamp = feats.get("features_computed_at")
    stale = True
    if isinstance(stamp, dict) and stamp.get("at"):
        try:
            stale = now - datetime.fromisoformat(str(stamp["at"])) > REFRESH_AFTER
        except ValueError:
            stale = True
    if ensure_fresh and stale:
        await recompute_features(user_id, now=now, conn=conn)
        rows = await conn.fetch("select key, value from user_features where user_id = $1", user_id)
        feats = {r["key"]: r["value"] for r in rows}

    interests = [
        {"dim": r["dim"], "weight": float(r["weight"]), "baseline": float(r["baseline"])}
        for r in await conn.fetch(
            "select dim, weight, baseline from user_interest_vector where user_id = $1 order by weight desc, dim",
            user_id,
        )
    ]
    memories = [
        {"id": str(r["id"]), "kind": r["kind"], "text": r["text"], "confidence": float(r["confidence"]), "source": r["source"] or ""}
        for r in await conn.fetch(
            "select id, kind, text, confidence, source from memories where user_id = $1 and not archived "
            "order by created_at desc, id limit $2",
            user_id, MEMORY_LIMIT,
        )
    ]  # fmt: skip
    bias = []
    for slug in SLUGS:
        item = feats.get(f"bias:{slug}")
        if isinstance(item, dict):
            bias.append({k: item.get(k) for k in ("name", "metric", "value", "state", "raw", "n")})
    revealed = _as_dict(feats.get("revealed_tier"))
    gap = _as_dict(feats.get("gap_days"))
    pinned = _as_dict(feats.get("pinned_metrics"))
    recent = [
        {
            "id": str(r["event_id"]),
            "ts": r["ts"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "type": r["type"],
            "text": describe_event(r["type"], r["entity"] or {}, r["props"] or {}),
        }
        for r in await conn.fetch(
            "select event_id, ts, type, entity, props from events where user_id = $1 order by ts desc limit $2",
            user_id, EVENT_LIMIT,
        )
    ]  # fmt: skip
    return {
        "interests": interests,
        "memories": memories,
        "bias": bias,
        "revealed_tier": revealed.get("value"),
        "gap": revealed.get("gap"),
        "gap_days": int(gap.get("value", 0) or 0),
        "gap_flag": bool(gap.get("flag", False)),
        "style_card": feats.get("style_card"),
        "pinned_metrics": pinned.get("metrics", []),
        "recent_events": recent,
    }


async def forget_memory(conn: Connection, user_id: UUID, memory_id: UUID) -> bool:
    """Delete one memory (profile control). Auto-derived ones are tombstoned so the next recompute does not
    re-create them. Returns False when the id is not the user's."""
    row = await conn.fetchrow(
        "delete from memories where user_id = $1 and id = $2 returning mem_key", user_id, memory_id
    )
    if row is None:
        return False
    if row["mem_key"]:
        await conn.execute(
            "insert into user_features (user_id, key, value, confidence, updated_at) "
            "values ($1, 'memory_tombstones', jsonb_build_object('keys', jsonb_build_array($2::text)), 1, now()) "
            "on conflict (user_id, key) do update set value = jsonb_build_object('keys', "
            "(select coalesce(jsonb_agg(distinct k), '[]'::jsonb) from jsonb_array_elements_text("
            "user_features.value -> 'keys' || jsonb_build_array($2::text)) as k)), updated_at = now()",
            user_id, row["mem_key"],
        )  # fmt: skip
    return True


async def reset_personalization(conn: Connection, user_id: UUID, *, include_events: bool = False) -> None:
    """Profile reset: forget everything learned (features, auto + explicit memories, tombstones), put the
    interest vector back to its onboarding baseline. Events are kept unless `include_events`."""
    await conn.execute("delete from user_features where user_id = $1", user_id)
    await conn.execute("delete from memories where user_id = $1", user_id)
    await conn.execute(
        "update user_interest_vector set weight = baseline, updated_at = now() where user_id = $1", user_id
    )
    if include_events:
        await conn.execute("delete from events where user_id = $1", user_id)
