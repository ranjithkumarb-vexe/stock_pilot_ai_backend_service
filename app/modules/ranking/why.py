"""`personalization_audit` rows and the "Why am I seeing this?" payload (SPEC 10.6.8, 10.7).

The explanation is assembled from the stored audit row only (features used, arm scores, rules checked, sources);
no model is involved. Basis: `profile` (pinned or profile-driven), `behaviour` (learned weights shaped the score)
or `cold_start` ("based on your setup": nothing learned yet).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from app.core.db import Connection
from app.modules.ranking.models import ARM_LABEL, Scored, dedupe_rules
from app.modules.ranking.scoring import W_INTEREST, W_MATERIALITY, W_THOMPSON

Basis = Literal["profile", "behaviour", "cold_start"]
REUSE_WINDOW = timedelta(minutes=10)
RETENTION = timedelta(days=30)
BEHAVIOUR_MIN_EVENTS = 5
# events that say the user has actually used the product (not setup / session bookkeeping)
BEHAVIOUR_EXCLUDED = ("onboarding_complete", "session_started", "rule_edited", "rule_override")

SUMMARY_PINNED = "Pinned: rule breaches always show and bypass learned ranking."
SUMMARY_COLD = "Based on your setup: ranking starts from your answers."
SUMMARY_PROFILE = "Based on your profile: this ranks high on materiality and your stated interests."


def determine_basis(pinned: bool, behaviour_events: int, learned_shift: float) -> Basis:
    """pinned -> profile; fewer than 5 behaviour events -> cold_start; learned weights moved the score -> behaviour."""
    if pinned:
        return "profile"
    if behaviour_events < BEHAVIOUR_MIN_EVENTS:
        return "cold_start"
    return "behaviour" if learned_shift >= 0.02 else "profile"


async def behaviour_event_count(conn: Connection, user_id: UUID, now: datetime) -> int:
    n = await conn.fetchval(
        "select count(*) from events where user_id = $1 and ts > $2 and not (type = any($3::text[]))",
        user_id,
        now - timedelta(days=90),
        list(BEHAVIOUR_EXCLUDED),
    )
    return int(n)


def audit_payload(s: Scored, rank: int, basis: Basis) -> dict[str, Any]:
    c = s.cand
    interest_dim = c.interest_dim
    return {
        "features_used": {
            "title": c.title,
            "pinned": s.pinned,
            "explored": s.explored,
            "materiality": {"value": round(s.materiality, 4), "weight": W_MATERIALITY},
            "learned": {
                "value": round(s.thompson, 4),
                "weight": W_THOMPSON,
                "arm": c.arm,
                "mean": s.arm_mean,
            },
            "interest": {"value": round(s.interest, 4), "weight": W_INTEREST, "dim": interest_dim},
            "sector": c.sector,
            "symbol": c.symbol,
            "sources": c.sources,
        },
        "arm_scores": {
            **s.detail,
            "effective_draw": round(s.thompson, 4),
            "score": round(s.score, 4),
            "rank": rank,
        },
        "rules_checked": c.rules_checked,
        "basis": basis,
    }


async def write_rows(
    conn: Connection, user_id: UUID, ranked: Sequence[Scored], basis_of: dict[str, Basis], now: datetime
) -> int:
    """One `personalization_audit` row per ranked item. A row that would repeat the previous one (same basis and
    score within 10 minutes) is not written again, so page reloads do not bloat the table."""
    if not ranked:
        return 0
    ids = [s.cand.id for s in ranked]
    recent = {
        (r["output_type"], r["output_id"]): r
        for r in await conn.fetch(
            "select distinct on (output_type, output_id) output_type, output_id, basis, arm_scores, created_at "
            "from personalization_audit where user_id = $1 and output_id = any($2::text[]) and created_at > $3 "
            "order by output_type, output_id, created_at desc",
            user_id,
            ids,
            now - REUSE_WINDOW,
        )
    }
    written = 0
    for rank, s in enumerate(ranked, start=1):
        basis = basis_of[s.cand.id]
        prev = recent.get((s.cand.kind, s.cand.id))
        if (
            prev is not None
            and prev["basis"] == basis
            and abs(float((prev["arm_scores"] or {}).get("score", -1.0)) - s.score) < 5e-5
        ):
            continue
        p = audit_payload(s, rank, basis)
        await conn.execute(
            "insert into personalization_audit (user_id, output_type, output_id, basis, features_used, arm_scores, "
            "rules_checked, created_at) values ($1, $2, $3, $4, $5, $6, $7, $8)",
            user_id, s.cand.kind, s.cand.id, basis, p["features_used"], p["arm_scores"], p["rules_checked"], now,
        )  # fmt: skip
        written += 1
    await conn.execute(
        "delete from personalization_audit where user_id = $1 and created_at < $2", user_id, now - RETENTION
    )
    return written


async def latest_row(conn: Connection, user_id: UUID, output_type: str, output_id: str) -> Any | None:
    return await conn.fetchrow(
        "select * from personalization_audit where user_id = $1 and output_type = $2 and output_id = $3 "
        "order by created_at desc, id limit 1",
        user_id,
        output_type,
        output_id,
    )


def build_why(row: Any) -> dict[str, Any]:
    """CONTRACT `why`: `{summary, top_features[{name, weight, detail}], rules_checked[{rule, passed}], sources[], basis}`."""
    f: dict[str, Any] = row["features_used"] or {}
    arm: dict[str, Any] = row["arm_scores"] or {}
    basis: str = row["basis"] or "profile"
    rules = dedupe_rules(row["rules_checked"] or [])  # older audit rows may repeat a rule
    sources = [str(s) for s in (f.get("sources") or [])]
    if f.get("pinned"):
        return {
            "summary": SUMMARY_PINNED,
            "top_features": [
                {
                    "name": "rule_breach",
                    "weight": 1.0,
                    "detail": "Computed by the deterministic risk engine from your own rules; learned ranking cannot hide it.",
                }
            ],
            "rules_checked": rules,
            "sources": sources or ["Risk engine"],
            "basis": "profile",
        }
    mat = f.get("materiality") or {}
    learned = f.get("learned") or {}
    interest = f.get("interest") or {}
    arm_name = str(learned.get("arm") or arm.get("arm") or "brief")
    label = ARM_LABEL.get(arm_name, arm_name)
    feats: list[dict[str, Any]] = []
    m_val = float(mat.get("value", 0.0))
    feats.append(
        {
            "name": "materiality",
            "weight": round(W_MATERIALITY * m_val, 4),
            "detail": f"Materiality {m_val:.2f}: scored by code from the event type, its size, your exposure and recency.",
        }
    )
    l_val = float(learned.get("value", 0.0))
    learned_detail = (
        f"Starting prior for {label} items, taken from your setup (nothing learned yet)."
        if basis == "cold_start"
        else f"Learned weight {l_val:.2f} from how you acted on past {label} items."
    )
    feats.append(
        {"name": "learned_preference", "weight": round(W_THOMPSON * l_val, 4), "detail": learned_detail}
    )
    i_val = float(interest.get("value", 0.0))
    dim = interest.get("dim")
    sector = f.get("sector")
    interest_detail = (
        f"Interest match: {sector} at {round(i_val * 100)}%."
        if dim and sector
        else f"No sector match, so a neutral interest weight of {round(i_val * 100)}% is used."
    )
    feats.append(
        {"name": "interest_match", "weight": round(W_INTEREST * i_val, 4), "detail": interest_detail}
    )
    if f.get("explored"):
        feats.append(
            {
                "name": "exploration",
                "weight": 0.0,
                "detail": "Shown occasionally even though this type ranks low, so the ranking can keep learning.",
            }
        )
    feats.sort(key=lambda x: -float(x["weight"]))
    top = feats[:3]
    score = float(arm.get("score", 0.0))
    if basis == "cold_start":
        summary = SUMMARY_COLD
    elif basis == "behaviour":
        summary = (
            f"Score {score:.2f} = 0.5 x materiality {m_val:.2f} + 0.3 x learned weight {l_val:.2f} "
            f"+ 0.2 x interest {i_val:.2f}."
        )
    else:
        summary = SUMMARY_PROFILE
    return {
        "summary": summary,
        "top_features": top,
        "rules_checked": rules,
        "sources": sources,
        "basis": basis,
    }
