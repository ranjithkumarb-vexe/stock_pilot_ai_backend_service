"""Candidate set for the ranker (SPEC 10.6.1): the user's live alerts, pending proposals and briefing items,
filtered by the risk engine (trades) and the user's exclusion lists (ESG / sector) BEFORE any scoring."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.errors import AppError
from app.core.logging import get_logger
from app.modules.alerts.store import unsnooze_due
from app.modules.alerts.view import alert_view
from app.modules.paper.context import EngineContext
from app.modules.paper.pricing import quotes_for
from app.modules.paper.state import PortfolioState, load_state
from app.modules.proposals.service import _jsonable, proposal_view
from app.modules.ranking.bandit import arm_for_alert_type
from app.modules.ranking.briefing import (
    MATERIALITY as BRIEFING_MATERIALITY,
)
from app.modules.ranking.briefing import (
    BriefingInputs,
    brief_ready_id,
    briefing_id,
    build_briefing,
)
from app.modules.ranking.models import Candidate, TradeSpec
from app.modules.risk import Portfolio, TradeProposal, compute_metrics, current_violations, validate

log = get_logger("app.ranking.candidates")

ALERT_LOOKBACK = timedelta(days=14)
MAX_ALERTS = 100
TRADE_MATERIALITY = 0.8
REVIEW_MATERIALITY = 0.55
BRIEF_READY_MATERIALITY = 0.4
BRIEF_READY_COUNT = 2

# ESG exclusion label -> case-insensitive keywords looked up in `tickers.industry` / `tickers.sector`
ESG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "tobacco": ("tobacco",),
    "weapons": ("aerospace & defense", "defense", "weapon"),
    "fossil fuels": ("oil", "gas", "coal", "energy"),
    "gambling": ("casino", "gambling", "gaming"),
}


def excluded_by(
    exclusions: Sequence[str], sector_excl: set[str], sector: str | None, industry: str | None
) -> str | None:
    """Return the matching exclusion label, else None. Pure."""
    if sector and sector in sector_excl:
        return f"sector:{sector}"
    hay = f"{sector or ''} {industry or ''}".lower()
    for label in exclusions:
        for kw in ESG_KEYWORDS.get(label.lower(), (label.lower(),)):
            if kw and kw in hay:
                return label
    return None


def alert_candidate(r: Any) -> Candidate:
    payload = r["payload"] or {}
    return Candidate(
        kind="alert",
        id=str(r["id"]),
        arm=arm_for_alert_type(r["type"]),
        materiality=float(r["materiality"]),
        critical=bool(r["critical"]),
        title=str(payload.get("title", "")),
        created_at=r["created_at"],
        item=alert_view(r),
        symbol=r["symbol"],
        sector=r["sector"],
        sources=[s for s in [payload.get("source_label")] if s],
        rules_checked=[
            {
                "rule": "Pinned by the risk engine" if r["critical"] else "Materiality scored by code",
                "passed": True,
            }
        ],
    )


async def alert_candidates(conn: Connection, user_id: UUID, now: datetime) -> list[Candidate]:
    await unsnooze_due(conn, user_id, now)
    rows = await conn.fetch(
        "select a.*, t.sector, t.industry from alerts a left join tickers t on t.symbol = a.symbol "
        "where a.user_id = $1 and a.state in ('new', 'opened') and a.created_at > $2 "
        "order by a.created_at desc limit $3",
        user_id,
        now - ALERT_LOOKBACK,
        MAX_ALERTS,
    )
    return [alert_candidate(r) for r in rows]


async def alert_candidate_by_id(conn: Connection, user_id: UUID, alert_id: UUID) -> Candidate | None:
    r = await conn.fetchrow(
        "select a.*, t.sector from alerts a left join tickers t on t.symbol = a.symbol "
        "where a.user_id = $1 and a.id = $2",
        user_id,
        alert_id,
    )
    return None if r is None else alert_candidate(r)


async def proposal_candidates(conn: Connection, user_id: UUID, now: datetime) -> list[Candidate]:
    rows = await conn.fetch(
        "select p.*, t.sector from proposals p left join tickers t on t.symbol = p.symbol "
        "where p.user_id = $1 and p.status = 'pending' and (p.expires_at is null or p.expires_at > $2) "
        "order by p.created_at desc",
        user_id,
        now,
    )
    return [proposal_candidate(r) for r in rows]


def proposal_candidate(r: Any) -> Candidate:
    view = _jsonable(proposal_view(r))
    pl = r["payload"] or {}
    trade: TradeSpec | None = None
    side, qty = pl.get("side"), pl.get("suggested_qty") or 0
    if r["type"] == "trade" and side in ("buy", "sell") and r["symbol"] and Decimal(str(qty)) > 0:
        trade = TradeSpec(r["symbol"], side, Decimal(str(qty)), r["sector"])
    checked = [
        {"rule": str(c.get("rule", "")), "passed": bool(c.get("passed", True))}
        for c in (r["rules_checked"] or [])
        if isinstance(c, dict)
    ]
    return Candidate(
        kind="proposal",
        id=str(r["id"]),
        arm="proposal",
        materiality=TRADE_MATERIALITY if r["type"] == "trade" else REVIEW_MATERIALITY,
        critical=False,
        title=str(pl.get("title", "")),
        created_at=r["created_at"],
        item=view,
        symbol=r["symbol"],
        sector=r["sector"],
        sources=list(pl.get("sources") or []),
        rules_checked=checked,
        trade=trade,
    )


async def proposal_candidate_by_id(conn: Connection, user_id: UUID, proposal_id: UUID) -> Candidate | None:
    r = await conn.fetchrow(
        "select p.*, t.sector from proposals p left join tickers t on t.symbol = p.symbol "
        "where p.user_id = $1 and p.id = $2",
        user_id,
        proposal_id,
    )
    return None if r is None else proposal_candidate(r)


async def briefing_candidates(
    conn: Connection,
    user_id: UUID,
    ctx: EngineContext,
    now: datetime,
    *,
    top_item_title: str | None,
    n_pending: int,
) -> list[Candidate]:
    """The daily briefing (always, when the user has a paper portfolio) and a few "brief ready" watchlist items."""
    out: list[Candidate] = []
    day = now.date()
    try:
        state = await load_state(conn, user_id, ctx)
    except AppError:  # no paper portfolio yet (not onboarded)
        return out
    metrics = compute_metrics(state.to_risk())
    breaches = [_breach_text(v) for v in current_violations(metrics, state.rules)]
    name = await conn.fetchval("select display_name from users where id = $1", user_id) or ""
    equity = float(state.equity)
    day_pct = float(metrics.day_change / state.equity * 100) if state.equity > 0 else 0.0
    inputs = BriefingInputs(
        first_name=str(name).split(" ")[0] if name else "",
        equity=equity,
        cash=float(state.cash),
        day_change_pct=day_pct,
        n_positions=len(state.positions),
        breaches=breaches,
        rules=state.rules.to_dict(),
        top_item_title=top_item_title,
        n_pending=n_pending,
    )
    item = build_briefing(inputs, day, now)
    out.append(
        Candidate(
            kind="briefing",
            id=str(briefing_id(user_id, day)),
            arm="brief",
            materiality=BRIEFING_MATERIALITY,
            critical=False,
            title=item["title"],
            created_at=now,
            item=item,
            sources=["Portfolio ledger and risk engine"],
            rules_checked=[{"rule": "All figures computed by code from your ledger", "passed": True}],
        )
    )
    held = {p.symbol for p in state.positions}
    rows = await conn.fetch(
        "select w.symbol, t.name, t.sector from watchlist_items w join tickers t on t.symbol = w.symbol "
        "where w.user_id = $1 and t.in_universe order by w.added_at, w.symbol",
        user_id,
    )
    ready = [r for r in rows if r["symbol"] not in held][:BRIEF_READY_COUNT]
    for r in ready:
        title = f"Research brief: {r['name']}"
        out.append(
            Candidate(
                kind="briefing",
                id=str(brief_ready_id(user_id, r["symbol"])),
                arm="brief",
                materiality=BRIEF_READY_MATERIALITY,
                critical=False,
                title=title,
                created_at=now,
                item={
                    "title": title,
                    "body": f"Open the source-linked research brief for {r['name']} ({r['sector'] or 'unclassified'}).",
                    "summary": "Research brief available",
                    "symbol": r["symbol"],
                    "generated_at": now.isoformat().replace("+00:00", "Z"),
                    "source_label": "Research brief",
                },
                symbol=r["symbol"],
                sector=r["sector"],
                sources=["Research brief"],
                rules_checked=[{"rule": "On your watchlist", "passed": True}],
            )
        )
    return out


def _breach_text(v: Any) -> str:
    cur = float(v.value) if v.value is not None else 0.0
    lim = float(v.limit) if v.limit is not None else 0.0
    names = {
        "pos": "max position",
        "sec": "max sector",
        "dd": "drawdown",
        "trades": "trades this week",
        "cash": "min cash buffer",
    }
    unit = "" if v.rule == "trades" else "%"
    return f"{names.get(v.rule, v.rule)} ({cur:.1f}{unit} vs {lim:.0f}{unit})"


async def exclusion_lists(conn: Connection, user_id: UUID) -> tuple[list[str], set[str]]:
    esg = await conn.fetchval("select esg_exclusions from profiles where user_id = $1", user_id) or []
    sectors = {
        r["sector"]
        for r in await conn.fetch("select sector from sector_prefs where user_id = $1 and pref = -1", user_id)
    }
    return list(esg), sectors


async def apply_filters(
    conn: Connection, user_id: UUID, ctx: EngineContext, cands: list[Candidate]
) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    """Risk-engine veto + exclusion lists. Criticals and already-held tickers are never excluded; a candidate
    whose trade `risk.validate` rejects is removed here, so it can never be ranked or shown (T-RISK-02)."""
    dropped: list[tuple[Candidate, str]] = []
    kept: list[Candidate] = []
    esg, sector_excl = await exclusion_lists(conn, user_id)
    held = {
        r["symbol"]
        for r in await conn.fetch("select symbol from positions where user_id = $1 and qty > 0", user_id)
    }
    industry = {r["symbol"]: r["industry"] for r in await conn.fetch("select symbol, industry from tickers")}
    state: PortfolioState | None = None
    if any(c.trade for c in cands):
        try:
            state = await load_state(conn, user_id, ctx)
        except AppError:
            state = None
    for c in cands:
        if c.critical:
            kept.append(c)
            continue
        if c.symbol and c.symbol not in held:
            hit = excluded_by(esg, sector_excl, c.sector, industry.get(c.symbol))
            if hit:
                dropped.append((c, f"excluded:{hit}"))
                continue
        if c.trade is not None:
            reason = await _risk_veto(conn, ctx, state, c)
            if reason:
                dropped.append((c, reason))
                continue
        kept.append(c)
    return kept, dropped


async def _risk_veto(
    conn: Connection, ctx: EngineContext, state: PortfolioState | None, c: Candidate
) -> str | None:
    trade = c.trade
    assert trade is not None
    if state is None:
        return "risk:no_portfolio"
    pos = state.position(trade.symbol)
    price: Decimal | None = pos.price if pos is not None else None
    if price is None:
        q = (await quotes_for(ctx.prices, [trade.symbol], conn)).get(trade.symbol)
        price = q.price if q is not None else None
    if price is None:
        return "risk:no_price"
    result = validate(
        TradeProposal(
            trade.symbol, trade.side, trade.qty, price, trade.sector or (pos.sector if pos else None)
        ),
        Portfolio(state.cash, state.to_risk().positions, state.trades_7d),
        state.rules,
    )
    c.rules_checked = [
        *c.rules_checked,
        *({"rule": ch.label, "passed": ch.ok} for ch in result.checks),
    ]
    if not result.allowed:
        first = result.violations[0] if result.violations else None
        return f"risk:{first.rule if first else 'blocked'}"
    return None
