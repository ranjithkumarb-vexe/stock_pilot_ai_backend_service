"""Bandit arms in `bandit_arms` (Beta posteriors per item type x context) and the paths that update them.

Rewards (SPEC 10.3 B/C): proposal approve 1.0 / modify 0.5 / reject 0.0, alert open 1.0 / snooze 0.3 / dismiss 0.0,
feed thumbs 1.0 / 0.0. Every update touches the type-level arm (context '') and, when the item has a sector, the
`sector:<S>` context arm. Guardrails: bounded posterior (`scoring.clamp_arm`), a per-user token bucket on updates
(a bot hammering reject cannot move the posterior more than the bucket allows), and criticals are never ranked by
the bandit at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.logging import get_logger
from app.core.ratelimit import TokenBucketLimiter
from app.modules.ranking.models import ALERT_TYPE_ARM, ARMS, Arm
from app.modules.ranking.scoring import DEFAULT_PRIOR, updated_arms

log = get_logger("app.ranking.bandit")

REWARD_OPEN = 1.0
REWARD_SNOOZE = 0.3
REWARD_DISMISS = 0.0
REWARD_USEFUL = 1.0
REWARD_NOT_USEFUL = 0.0

# per-user event rate limit for posterior updates: 20 burst, then one update per 3 s (SPEC 10.6 guardrail 7)
LIMIT_BURST = 20
LIMIT_PER_S = 1.0 / 3.0


class RewardLimiter:
    def __init__(
        self,
        burst: int = LIMIT_BURST,
        per_s: float = LIMIT_PER_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bucket = TokenBucketLimiter(burst, per_s, lambda: monotonic())
        self.tripped = 0

    def allow(self, user_id: UUID) -> bool:
        if self._bucket.check(str(user_id)) > 0:
            self.tripped += 1
            return False
        return True


LIMITER = RewardLimiter()


def set_limiter(limiter: RewardLimiter) -> RewardLimiter:
    """Swap the process-wide limiter (tests); returns the previous one."""
    global LIMITER
    prev, LIMITER = LIMITER, limiter
    return prev


def cold_start_priors(tier: int | None, goals: list[str] | None = None) -> dict[str, Arm]:
    """Persona-cluster priors from the questionnaire (SPEC 10.6.5): weak Beta(2,2)-style, nudged by the stated
    tier. Conservative users start with breach/earnings/filing alerts a little ahead of volatility moves;
    aggressive users the other way round. No cross-user data is used."""
    pri = dict.fromkeys(ARMS, DEFAULT_PRIOR)
    t = tier or 3
    if t <= 2:
        pri.update(breach=Arm(3, 2), earnings=Arm(3, 2), filing=Arm(3, 2), move=Arm(2, 3), mirror=Arm(2.5, 2))
    elif t >= 4:
        pri.update(move=Arm(3, 2), earnings=Arm(3, 2), filing=Arm(3, 2), breach=Arm(3, 2), mirror=Arm(2, 2.5))
    else:
        pri.update(breach=Arm(3, 2))
    if goals and any(g.lower() == "income" for g in goals):
        pri.update(brief=Arm(2.5, 2), earnings=Arm(3, 2))
    return pri


async def load_priors(conn: Connection, user_id: UUID) -> dict[str, Arm]:
    row = await conn.fetchrow("select risk_tier_stated, goals from profiles where user_id = $1", user_id)
    if row is None:
        return cold_start_priors(None)
    return cold_start_priors(int(row["risk_tier_stated"]), list(row["goals"] or []))


async def load_arms(conn: Connection, user_id: UUID) -> dict[tuple[str, str], Arm]:
    rows = await conn.fetch("select arm, context, alpha, beta from bandit_arms where user_id = $1", user_id)
    return {(r["arm"], r["context"]): Arm(float(r["alpha"]), float(r["beta"])) for r in rows}


async def _write(conn: Connection, user_id: UUID, arm: str, ctx: str, new: Arm, now: datetime) -> None:
    await conn.execute(
        "insert into bandit_arms (user_id, arm, context, alpha, beta, updated_at) values ($1, $2, $3, $4, $5, $6) "
        "on conflict (user_id, arm, context) do update set alpha = excluded.alpha, beta = excluded.beta, "
        "updated_at = excluded.updated_at",
        user_id, arm, ctx, round(new.alpha, 4), round(new.beta, 4), now,
    )  # fmt: skip


async def apply_reward(
    conn: Connection,
    user_id: UUID,
    arm: str,
    reward: float,
    *,
    sector: str | None = None,
    now: datetime,
    limiter: RewardLimiter | None = None,
) -> bool:
    """Update the type arm (and the sector context arm). Returns False when the per-user limiter dropped it."""
    if arm not in ARMS:
        log.warning("unknown bandit arm ignored", extra={"arm": arm})
        return False
    if not (limiter or LIMITER).allow(user_id):
        log.info("bandit update dropped by the rate limit", extra={"arm": arm})
        return False
    arms = await load_arms(conn, user_id)
    priors = await load_priors(conn, user_id)
    for (name, ctx), new in updated_arms(arms, priors, arm, sector, reward).items():
        await _write(conn, user_id, name, ctx, new, now)
    return True


def arm_for_alert_type(alert_type: str) -> str:
    return ALERT_TYPE_ARM.get(alert_type, "brief")


async def sector_of(conn: Connection, symbol: str | None) -> str | None:
    if not symbol:
        return None
    value = await conn.fetchval("select sector from tickers where symbol = $1", symbol)
    return str(value) if value else None


async def on_proposal_decision(
    conn: Connection, user_id: UUID, proposal: dict[str, Any], reward: float
) -> None:
    """`proposals.hooks.register_decision_hook` target: approve 1.0 / modify 0.5 / reject 0.0 for the proposal arm."""
    sector = await sector_of(conn, proposal.get("symbol"))
    await apply_reward(conn, user_id, "proposal", reward, sector=sector, now=_now(proposal))


def _now(proposal: dict[str, Any]) -> datetime:
    from datetime import UTC

    raw = proposal.get("decided_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def register_hooks() -> None:
    """Idempotent: attach the bandit to the proposal workflow."""
    from app.modules.proposals import hooks

    hooks.register_decision_hook(on_proposal_decision)
