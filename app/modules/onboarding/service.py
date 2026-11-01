"""Create a user's workspace from the onboarding answers (FR-01). One transaction, all-or-nothing."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.audit import write_audit
from app.core.db import Connection
from app.core.errors import conflict, validation_error
from app.modules.onboarding.content import (
    ESG_EXCLUSIONS,
    SUPPORTED_CURRENCY,
    SUPPORTED_REGION,
    explanation_for,
    score_answers,
    tier_rules,
    valid_scores,
)
from app.modules.onboarding.schemas import OnboardingRequest
from app.modules.paper.context import EngineContext
from app.modules.profile import service as profile_service
from app.modules.tradingcommon.events import emit_event
from app.modules.watchlist.service import MAX_WATCHLIST, list_watchlist, unsupported


async def complete_onboarding(
    conn: Connection, user_id: UUID, body: OnboardingRequest, ctx: EngineContext
) -> dict[str, Any]:
    now = ctx.clock.now()
    if not body.disclaimer_ack:
        raise validation_error("The disclaimer must be acknowledged", {"field": "disclaimer_ack"})
    if body.region != SUPPORTED_REGION or body.currency != SUPPORTED_CURRENCY:
        raise validation_error(
            "Only region US and currency USD are supported",
            {"region": body.region, "currency": body.currency},
        )
    for i, score in enumerate(body.answers):
        if score not in valid_scores(i):
            raise validation_error(f"Answer {i + 1} is not one of the offered options", {"question": i + 1})
    bad_esg = [e for e in body.esg_exclusions if e not in ESG_EXCLUSIONS]
    if bad_esg:
        raise validation_error("Unknown ESG exclusion", {"values": bad_esg})
    if set(body.likes) & set(body.excludes):
        raise validation_error("A sector cannot be both liked and excluded")
    sectors = {
        r["sector"]
        for r in await conn.fetch(
            "select distinct sector from tickers where in_universe and sector is not null"
        )
    }
    bad_sec = [s for s in [*body.likes, *body.excludes] if s not in sectors]
    if bad_sec:
        raise validation_error("Unknown sector", {"sectors": bad_sec})
    if len(body.watchlist) > MAX_WATCHLIST:
        raise validation_error(f"Watchlist is limited to {MAX_WATCHLIST} symbols")
    known = {
        r["symbol"]
        for r in await conn.fetch(
            "select symbol from tickers where in_universe and symbol = any($1::text[])", body.watchlist
        )
    }
    for sym in body.watchlist:
        if sym not in known:
            raise unsupported(sym)

    row = await conn.fetchrow("select onboarded_at from users where id = $1 for update", user_id)
    if (
        row is None
        or row["onboarded_at"] is not None
        or await conn.fetchval("select 1 from profiles where user_id = $1", user_id)
    ):
        raise conflict("Onboarding is already complete")

    result = score_answers(body.answers)
    rules = tier_rules(result.tier)
    await conn.execute(
        """
        insert into profiles (user_id, risk_tier_stated, horizon, goals, experience, region, currency,
                              esg_exclusions, disclaimer_ack_at, created_at, updated_at)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, $9)
        """,
        user_id, result.tier, body.horizon, list(body.goals), body.experience, body.region, body.currency,
        list(body.esg_exclusions), now,
    )  # fmt: skip
    await write_audit(
        conn,
        user_id=user_id,
        action="disclaimer.ack",
        entity="profile",
        entity_id=str(user_id),
        details={"source": "onboarding"},
    )
    for key, val in rules.to_dict().items():
        await conn.execute(
            "insert into risk_rules (user_id, rule_key, value, source, updated_at) values ($1, $2, $3, 'default', $4)",
            user_id, key, Decimal(str(val)), now,
        )  # fmt: skip
    for s in body.likes:
        await conn.execute("insert into sector_prefs (user_id, sector, pref) values ($1, $2, 1)", user_id, s)
    for s in body.excludes:
        await conn.execute("insert into sector_prefs (user_id, sector, pref) values ($1, $2, -1)", user_id, s)
    for k, sym in enumerate(body.watchlist):
        from datetime import timedelta

        await conn.execute(
            "insert into watchlist_items (user_id, symbol, added_at) values ($1, $2, $3)",
            user_id, sym, now + timedelta(microseconds=k),
        )  # fmt: skip

    pf_id = await conn.fetchval(
        "insert into portfolios (user_id, name, starting_cash, created_at) values ($1, 'Main', $2, $3) returning id",
        user_id, body.starting_cash, now,
    )  # fmt: skip
    await conn.execute(
        "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount) values ($1, $2, $3, 'deposit', $4)",
        pf_id, user_id, now, body.starting_cash,
    )  # fmt: skip
    await conn.execute(
        "insert into equity_snapshots (portfolio_id, user_id, ts, equity, cash, spy_equity_norm) values ($1, $2, $3, $4, $4, $4)",
        pf_id, user_id, now, body.starting_cash,
    )  # fmt: skip
    await profile_service.seed_learning_state(
        conn, user_id, now, tier=result.tier, goals=list(body.goals), horizon=body.horizon,
        experience=body.experience, likes=list(body.likes), esg=list(body.esg_exclusions),
    )  # fmt: skip
    await conn.execute("update users set onboarded_at = $2 where id = $1", user_id, now)
    await write_audit(
        conn, user_id=user_id, action="onboarding.complete", entity="profile", entity_id=str(user_id),
        details={"tier": result.tier, "watchlist": len(body.watchlist), "starting_cash": float(body.starting_cash)},
    )  # fmt: skip
    await emit_event(conn, user_id=user_id, ts=now, type="onboarding_complete", props={"tier": result.tier})
    return {
        "profile": await profile_service.profile_object(conn, user_id),
        "risk_tier": result.tier,
        "tier_label": result.label,
        "explanation": explanation_for(result, rules),
        "rules": rules.to_dict(),
        "watchlist": await list_watchlist(conn, user_id, ctx),
    }
