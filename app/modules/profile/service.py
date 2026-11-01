"""Profile read/write logic. Every write appends `audit_log` and, where behaviour-relevant, an `events` row."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.audit import write_audit
from app.core.db import Connection
from app.core.errors import not_found
from app.modules.onboarding.content import (
    TIER_LABELS,
    default_style,
    initial_interests,
    interest_dim,
)
from app.modules.paper.context import EngineContext
from app.modules.paper.state import load_state
from app.modules.personalization import api_helpers as learned_api
from app.modules.risk import Rules, compute_metrics, default_rules_for_tier
from app.modules.tradingcommon.events import emit_event

RULE_KEYS = ("pos", "sec", "dd", "trades", "cash")
BIAS_ORDER = (
    ("overtrading", "Overtrading", "Trades per week"),
    ("disposition", "Disposition effect", "PGR - PLR"),
    ("concentration", "Concentration", "HHI"),
    ("herding", "Herding", "Buys within 2 days of a spike"),
    ("loss_aversion", "Loss aversion", "Reject ratio, losers vs winners"),
)
_NO_DATA = {
    "overtrading": "No data yet",
    "disposition": "Needs 10 sells",
    "herding": "No data yet",
    "loss_aversion": "No data yet",
}


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


async def profile_object(conn: Connection, user_id: UUID) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        select p.*, u.display_name, u.email, u.onboarded_at,
               (select starting_cash from portfolios where user_id = p.user_id order by created_at limit 1) as starting_cash
          from profiles p join users u on u.id = p.user_id where p.user_id = $1
        """,
        user_id,
    )
    if row is None:
        raise not_found("Profile not found - complete onboarding first")
    prefs = await conn.fetch(
        "select sector, pref from sector_prefs where user_id = $1 order by sector", user_id
    )
    tier = int(row["risk_tier_stated"])
    return {
        "display_name": row["display_name"],
        "email": row["email"],
        "risk_tier_stated": tier,
        "tier_label": TIER_LABELS[tier],
        "horizon": row["horizon"],
        "goals": list(row["goals"]),
        "experience": row["experience"],
        "region": row["region"],
        "currency": row["currency"],
        "esg_exclusions": list(row["esg_exclusions"]),
        "likes": [r["sector"] for r in prefs if r["pref"] == 1],
        "excludes": [r["sector"] for r in prefs if r["pref"] == -1],
        "starting_cash": None if row["starting_cash"] is None else float(row["starting_cash"]),
        "disclaimer_ack_at": None if row["disclaimer_ack_at"] is None else iso(row["disclaimer_ack_at"]),
        "onboarded_at": None if row["onboarded_at"] is None else iso(row["onboarded_at"]),
        "created_at": iso(row["created_at"]),
    }


async def get_rules(conn: Connection, user_id: UUID) -> tuple[Rules, dict[str, str]]:
    rows = await conn.fetch("select rule_key, value, source from risk_rules where user_id = $1", user_id)
    have = {r["rule_key"]: (r["value"], r["source"]) for r in rows}
    tier = await conn.fetchval("select risk_tier_stated from profiles where user_id = $1", user_id)
    base = default_rules_for_tier(int(tier or 3)).to_dict()
    merged = {k: have[k][0] if k in have else base[k] for k in RULE_KEYS}
    sources = {k: have[k][1] if k in have else "default" for k in RULE_KEYS}
    return Rules.from_mapping(merged), sources


def event_text(typ: str, entity: dict[str, Any], props: dict[str, Any]) -> str:
    t = entity.get("ticker")
    if typ == "ticker_viewed" and t:
        return f"Viewed {t} brief"
    if typ == "section_expanded":
        secs = round(float(props.get("dwell_ms", 0)) / 1000)
        return f"Expanded a brief section for {secs}s" if secs else "Expanded a brief section"
    if typ in {"order_filled", "order_placed"} and t:
        return f"{'Filled' if typ == 'order_filled' else 'Placed'} {props.get('side', '')} {props.get('qty', '')} {t}".replace(
            "  ", " "
        ).strip()
    if typ == "proposal_rejected":
        return f"Rejected a proposal{f' for {t}' if t else ''}" + (
            f" (reason: {props['reason']})" if props.get("reason") else ""
        )
    if typ == "rule_edited":
        return f"Edited rule {entity.get('rule', '')}: {props.get('old')} to {props.get('new')}"
    if typ == "onboarding_complete":
        return "Completed onboarding and acknowledged the disclaimer"
    return typ.replace("_", " ").capitalize() + (f" {t}" if t else "")


async def get_profile_full(conn: Connection, user_id: UUID, ctx: EngineContext) -> dict[str, Any]:
    """CONTRACT `/profile`. The *learned* parts (interests, memories, bias, revealed tier, gap, recent events,
    style card) come from stream C's `personalization.api_helpers.get_profile_learned` (it recomputes features
    when none exist or they are older than 10 minutes); profile, rules and the live concentration row are ours."""
    profile = await profile_object(conn, user_id)
    rules, sources = await get_rules(conn, user_id)
    learned = await learned_api.get_profile_learned(conn, user_id, now=ctx.clock.now())
    style = dict(
        learned["style_card"] or default_style(profile["experience"] or "Intermediate", profile["goals"])
    )
    style.update(style.get("explicit") or {})
    interests = [
        {
            "dim": i["dim"],
            "label": i["dim"].split(":", 1)[-1].title()
            if i["dim"].startswith("factor:")
            else i["dim"].split(":", 1)[-1],
            "weight": i["weight"],
            "baseline": i["baseline"],
        }
        for i in learned["interests"]
    ]
    bias: list[dict[str, Any]] = []
    by_name = {b["name"]: b for b in learned["bias"]}
    for slug, name, metric in BIAS_ORDER:
        item = by_name.get(name)
        if item is not None and item.get("state") in {"ok", "watch", "flag"}:
            bias.append(
                {
                    "name": name,
                    "metric": item.get("metric") or metric,
                    "value": str(item.get("value", "")),
                    "state": item["state"],
                }
            )
        elif slug == "concentration":
            try:
                state = await load_state(conn, user_id, ctx)
                hhi = compute_metrics(state.to_risk()).hhi
            except Exception:
                hhi = None
            bias.append(
                {
                    "name": name,
                    "metric": metric,
                    "value": f"{hhi:.2f} (flag above 0.25)" if hhi else "No positions",
                    "state": ("flag" if hhi > 0.25 else "ok") if hhi else "ok",
                }
            )
        else:
            bias.append({"name": name, "metric": metric, "value": _NO_DATA[slug], "state": "ok"})
    revealed = learned["revealed_tier"]
    return {
        "profile": profile,
        "style_card": style,
        "rules": rules.to_dict(),
        "rule_sources": sources,
        "interests": interests,
        "memories": learned["memories"],
        "bias": bias,
        "stated_tier": profile["risk_tier_stated"],
        "revealed_tier": float(revealed) if revealed is not None else float(profile["risk_tier_stated"]),
        "gap_days": learned["gap_days"],
        "gap_flag": learned["gap_flag"],
        "pinned_metrics": learned["pinned_metrics"],
        "recent_events": learned["recent_events"],
    }


# ----------------------------------------------------------------------------------- writes


async def patch_profile(
    conn: Connection, user_id: UUID, now: datetime, changes: dict[str, Any], valid_sectors: set[str]
) -> list[str]:
    """Apply a validated partial update. Returns the changed field names."""
    changed: list[str] = []
    updates = {
        "horizon": "update profiles set horizon = $2, updated_at = $3 where user_id = $1",
        "experience": "update profiles set experience = $2, updated_at = $3 where user_id = $1",
        "region": "update profiles set region = $2, updated_at = $3 where user_id = $1",
        "currency": "update profiles set currency = $2, updated_at = $3 where user_id = $1",
        "goals": "update profiles set goals = $2, updated_at = $3 where user_id = $1",
        "esg_exclusions": "update profiles set esg_exclusions = $2, updated_at = $3 where user_id = $1",
    }
    for key, sql in updates.items():
        if key in changes:
            value = list(changes[key]) if key in {"goals", "esg_exclusions"} else changes[key]
            await conn.execute(sql, user_id, value, now)
            changed.append(key)
    if "display_name" in changes:
        await conn.execute(
            "update users set display_name = $2 where id = $1", user_id, changes["display_name"]
        )
        changed.append("display_name")
    if "likes" in changes or "excludes" in changes:
        cur = {
            r["sector"]: r["pref"]
            for r in await conn.fetch("select sector, pref from sector_prefs where user_id = $1", user_id)
        }
        likes = set(changes["likes"]) if "likes" in changes else {s for s, p in cur.items() if p == 1}
        excludes = (
            set(changes["excludes"]) if "excludes" in changes else {s for s, p in cur.items() if p == -1}
        )
        await conn.execute("delete from sector_prefs where user_id = $1", user_id)
        for s in sorted(likes):
            await conn.execute(
                "insert into sector_prefs (user_id, sector, pref) values ($1, $2, 1)", user_id, s
            )
        for s in sorted(excludes):
            await conn.execute(
                "insert into sector_prefs (user_id, sector, pref) values ($1, $2, -1)", user_id, s
            )
        changed += [k for k in ("likes", "excludes") if k in changes]
    style_keys = {"tone", "depth", "jargon", "metric", "language"}
    if style_keys & changes.keys():
        cur_style = (
            await conn.fetchval(
                "select value from user_features where user_id = $1 and key = 'style_card'", user_id
            )
            or {}
        )
        picked = {k: changes[k] for k in style_keys & changes.keys()}
        # `explicit` records what the USER chose: the learned-feature job re-derives jargon/depth/metric from
        # behaviour, but an explicit choice always wins on read (and should win in the job - see CR-13)
        new_style = {**cur_style, **picked, "explicit": {**(cur_style.get("explicit") or {}), **picked}}
        await conn.execute(
            """
            insert into user_features (user_id, key, value, confidence, updated_at) values ($1, 'style_card', $2, 1, $3)
            on conflict (user_id, key) do update set value = excluded.value, confidence = 1, updated_at = excluded.updated_at
            """,
            user_id,
            new_style,
            now,
        )
        changed += sorted(style_keys & changes.keys())
    if changed:
        await write_audit(
            conn,
            user_id=user_id,
            action="profile.update",
            entity="profile",
            entity_id=str(user_id),
            details={"fields": changed},
        )
        await emit_event(conn, user_id=user_id, ts=now, type="profile_edited", props={"fields": changed})
    return changed


def _loosened(key: str, old: Decimal, new: Decimal) -> bool:
    return new < old if key == "cash" else new > old


async def put_rules(conn: Connection, user_id: UUID, now: datetime, new_rules: Rules) -> dict[str, Any]:
    """Full replace. Each changed rule: upsert (source 'user' unless equal to the tier default), `audit_log`
    row and a `rule_edited` event. Loosening a rule returns a reflective prompt, never silently."""
    tier = int(await conn.fetchval("select risk_tier_stated from profiles where user_id = $1", user_id) or 3)
    defaults = default_rules_for_tier(tier)
    old, _ = await get_rules(conn, user_id)
    changed: list[dict[str, Any]] = []
    warnings: list[str] = []
    for key in RULE_KEYS:
        o, n = Decimal(str(getattr(old, key))), Decimal(str(getattr(new_rules, key)))
        d = Decimal(str(getattr(defaults, key)))
        await conn.execute(
            """
            insert into risk_rules (user_id, rule_key, value, source, updated_at) values ($1, $2, $3, $4, $5)
            on conflict (user_id, rule_key) do update set value = excluded.value, source = excluded.source,
                updated_at = case when risk_rules.value = excluded.value then risk_rules.updated_at else excluded.updated_at end
            """,
            user_id, key, n, "default" if n == d else "user", now,
        )  # fmt: skip
        if o == n:
            continue
        direction = "loosen" if _loosened(key, o, n) else "tighten"
        changed.append({"rule": key, "old": float(o), "new": float(n), "direction": direction})
        await write_audit(
            conn,
            user_id=user_id,
            action="rule.edit",
            entity="risk_rule",
            entity_id=key,
            details={"old": float(o), "new": float(n), "direction": direction},
        )
        await emit_event(
            conn,
            user_id=user_id,
            ts=now,
            type="rule_edited",
            entity={"rule": key},
            props={"old": float(o), "new": float(n), "direction": direction},
        )
        if direction == "loosen":
            warnings.append(
                f"You loosened '{key}' from {o:g} to {n:g}. Consider noting why; the risk engine now uses the new limit."
            )
    if new_rules.sec < new_rules.pos:
        warnings.append(
            "Your sector limit is lower than your single-position limit, so the position limit can never bind on its own."
        )
    return {"rules": new_rules.to_dict(), "changed": changed, "warnings": warnings}


async def forget_memory(conn: Connection, user_id: UUID, now: datetime, memory_id: UUID) -> None:
    if not await learned_api.forget_memory(conn, user_id, memory_id):  # tombstones auto-derived memories
        raise not_found("Memory not found")
    await write_audit(
        conn, user_id=user_id, action="memory.forget", entity="memory", entity_id=str(memory_id)
    )
    await emit_event(
        conn, user_id=user_id, ts=now, type="memory_forgotten", entity={"memory_id": str(memory_id)}
    )


def cold_start_memories(tier: int, goals: list[str], horizon: str, esg: list[str]) -> list[dict[str, Any]]:
    mems = [
        {
            "kind": "semantic",
            "text": f"Stated risk tier {tier} ({TIER_LABELS[tier]}) from the questionnaire.",
            "conf": 0.6,
        },
        {"kind": "semantic", "text": f"Goals: {', '.join(goals)}. Horizon: {horizon}.", "conf": 0.9},
    ]
    if esg:
        mems.append({"kind": "semantic", "text": f"Excludes: {', '.join(esg)}.", "conf": 1.0})
    return mems


async def seed_learning_state(
    conn: Connection, user_id: UUID, now: datetime, *, tier: int, goals: list[str], horizon: str,
    experience: str, likes: list[str], esg: list[str],
) -> None:  # fmt: skip
    """Cold-start learned state: interest vector (baseline == weight), bandit arms (1,1), style card, memories."""
    from app.modules.onboarding.content import BANDIT_ARMS

    for name, w in initial_interests(likes, goals):
        await conn.execute(
            "insert into user_interest_vector (user_id, dim, weight, baseline, updated_at) values ($1, $2, $3, $3, $4) "
            "on conflict (user_id, dim) do nothing",
            user_id, interest_dim(name), Decimal(str(w)), now,
        )  # fmt: skip
    for arm in BANDIT_ARMS:
        await conn.execute(
            "insert into bandit_arms (user_id, arm, context, alpha, beta, updated_at) values ($1, $2, '', 1, 1, $3) on conflict do nothing",
            user_id, arm, now,
        )  # fmt: skip
    feats: list[tuple[str, dict[str, Any], Decimal, int | None]] = [
        ("style_card", default_style(experience, goals), Decimal("0.6"), None),
        ("revealed_tier", {"value": float(tier)}, Decimal("0.3"), 30),
        ("gap_days", {"value": 0}, Decimal("0.3"), 30),
    ]
    for key, value, conf, half in feats:
        await conn.execute(
            "insert into user_features (user_id, key, value, confidence, half_life_days, updated_at) values ($1, $2, $3, $4, $5, $6) "
            "on conflict (user_id, key) do update set value = excluded.value, confidence = excluded.confidence, updated_at = excluded.updated_at",
            user_id, key, value, conf, half, now,
        )  # fmt: skip
    for m in cold_start_memories(tier, goals, horizon, esg):
        await conn.execute(
            "insert into memories (user_id, kind, text, source, confidence, created_at) values ($1, $2, $3, 'Onboarding', $4, $5)",
            user_id, m["kind"], m["text"], Decimal(str(m["conf"])), now,
        )  # fmt: skip


async def reset_learning(conn: Connection, user_id: UUID, now: datetime) -> dict[str, int]:
    """Forget everything learned (FR-09). Stream C's `reset_personalization` clears features + memories (+ tombstones)
    and restores the interest baseline; we also clear the bandit arms and ranking audit and rebuild the cold-start
    state (style card, onboarding memories, arms at 1/1) from the profile. Profile, rules, watchlist, portfolio and
    the event history are untouched."""
    counts = {
        "memories": int(await conn.fetchval("select count(*) from memories where user_id = $1", user_id)),
        "user_features": int(await conn.fetchval("select count(*) from user_features where user_id = $1", user_id)),
        "bandit_arms": int(await conn.fetchval("select count(*) from bandit_arms where user_id = $1", user_id)),
        "personalization_audit": int(await conn.fetchval("select count(*) from personalization_audit where user_id = $1", user_id)),
    }  # fmt: skip
    await learned_api.reset_personalization(conn, user_id, include_events=False)
    await conn.execute("delete from bandit_arms where user_id = $1", user_id)
    await conn.execute("delete from personalization_audit where user_id = $1", user_id)
    p = await profile_object(conn, user_id)
    await seed_learning_state(
        conn, user_id, now, tier=p["risk_tier_stated"], goals=p["goals"], horizon=p["horizon"] or "3-10 years",
        experience=p["experience"] or "Intermediate", likes=p["likes"], esg=p["esg_exclusions"],
    )  # fmt: skip
    await write_audit(
        conn,
        user_id=user_id,
        action="profile.reset",
        entity="profile",
        entity_id=str(user_id),
        details={"cleared": counts},
    )
    await emit_event(
        conn, user_id=user_id, ts=now, type="profile_reset", props={"cleared": sum(counts.values())}
    )
    return counts
