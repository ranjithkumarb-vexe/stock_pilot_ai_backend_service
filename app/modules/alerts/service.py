"""Alert evaluation and actions on the database (BE-17).

`evaluate_user` runs every detector for ONE user inside that user's RLS transaction and inserts what is new:

    detect -> materiality (deterministic) -> dedupe key / 24 h suppression -> insert -> (caller) deliver via SSE

* breach alerts come from the real risk engine (`risk.current_violations`); an alert whose breach has been cleared
  is resolved automatically (the user can never dismiss a critical alert, the engine closes it);
* non-critical alerts must clear the user's adaptive delivery floor; criticals bypass it (T-ALERT-03);
* unusual moves use the user's adaptive `k` and are suppressed when the quote is stale or the market is closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.audit import write_audit
from app.core.db import Connection
from app.core.errors import AppError, conflict, not_found
from app.core.logging import get_logger
from app.modules.alerts import adaptive
from app.modules.alerts.detectors import (
    AlertDraft,
    FilingSignal,
    daily_sigma,
    detect_breaches,
    detect_concentration,
    detect_earnings,
    detect_filings,
    detect_move,
    largest_move_note,
)
from app.modules.alerts.earnings import EarningsSource
from app.modules.alerts.store import SNOOZE, get_alert, unsnooze_due
from app.modules.alerts.view import alert_view
from app.modules.paper.context import EngineContext
from app.modules.paper.pricing import quotes_for
from app.modules.paper.state import load_state
from app.modules.ranking import bandit
from app.modules.risk import compute_metrics

log = get_logger("app.alerts.service")

SUPPRESSION = timedelta(hours=24)
THRESHOLD_KEY = "alert_threshold"
HISTORY_DAYS = 400
ALLOWED_FRESHNESS = frozenset({"live", "delayed"})  # marketdata.freshness.allows_move_alerts
ACTIONS = ("open", "dismiss", "snooze", "restore")
LIVE_REPEAT_TYPES = (
    "breach",
    "concentration",
)  # a condition that persists is never re-raised while its alert is live


@dataclass(slots=True)
class EvalContext:
    """What one monitoring cycle shares between users."""

    now: datetime
    ctx: EngineContext
    signals: list[FilingSignal] = field(default_factory=list)
    earnings: EarningsSource | None = None
    move_allowed: Any = None  # optional callable(symbol) -> bool (MarketDataService.move_alerts_allowed)


@dataclass(slots=True)
class EvalResult:
    created: list[dict[str, Any]] = field(default_factory=list)
    drafts: int = 0
    resolved: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    k: float = adaptive.K_START

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


# ------------------------------------------------------------------ adaptive threshold
async def load_threshold(conn: Connection, user_id: UUID) -> dict[str, Any]:
    val = await conn.fetchval(
        "select value from user_features where user_id = $1 and key = $2", user_id, THRESHOLD_KEY
    )
    base: dict[str, Any] = {"k": adaptive.K_START, "fatigue": 0.0, "ctr": 0.0, "acted": 0, "sent": 0}
    if isinstance(val, dict):
        base.update(val)
    base["k"] = adaptive.clamp_k(float(base["k"]))
    base["min_materiality"] = adaptive.min_materiality(base["k"])
    return base


async def alert_counts(conn: Connection, user_id: UUID, now: datetime) -> adaptive.Counts:
    r = await conn.fetchrow(
        "select count(*) as sent, count(*) filter (where state = 'opened') as opened, "
        "count(*) filter (where state = 'dismissed' and not (payload ? 'resolved')) as dismissed, "
        "count(*) filter (where state = 'snoozed') as snoozed "
        "from alerts where user_id = $1 and not critical and created_at > $2",
        user_id,
        now - timedelta(days=adaptive.WINDOW_DAYS),
    )
    assert r is not None
    return adaptive.Counts(int(r["sent"]), int(r["opened"]), int(r["dismissed"]), int(r["snoozed"]))


async def adapt_threshold(conn: Connection, user_id: UUID, now: datetime) -> dict[str, Any]:
    """Re-derive fatigue / CTR from the last 7 days and take one step on `k` (called after a non-critical action)."""
    cur = await load_threshold(conn, user_id)
    counts = await alert_counts(conn, user_id, now)
    k = adaptive.next_k(float(cur["k"]), counts)
    state = {
        "k": k,
        "fatigue": round(adaptive.fatigue_index(counts), 4),
        "ctr": round(adaptive.click_through(counts), 4),
        "acted": counts.acted,
        "sent": counts.sent,
        "updated_at": now.isoformat(),
    }
    await conn.execute(
        "insert into user_features (user_id, key, value, confidence, half_life_days, updated_at) "
        "values ($1, $2, $3, 1, 7, $4) on conflict (user_id, key) do update set value = excluded.value, "
        "updated_at = excluded.updated_at",
        user_id, THRESHOLD_KEY, state, now,
    )  # fmt: skip
    state["min_materiality"] = adaptive.min_materiality(k)
    return state


# ------------------------------------------------------------------ seeded rows
def _infer_breach(title: str) -> tuple[str, str, str] | None:
    if title.startswith("Position cap breached: "):
        sym = title.removeprefix("Position cap breached: ").strip()
        return "pos", sym, f"pos:{sym}"
    if title.startswith("Sector rule breached: "):
        sec = title.removeprefix("Sector rule breached: ").strip()
        return "sec", sec, f"sec:{sec}"
    return None


async def adopt_seed_breaches(conn: Connection, user_id: UUID) -> int:
    """Breach alerts inserted by `scripts/seed.py` carry no breach key. Give them the engine's key so the real
    risk engine neither duplicates nor orphans them."""
    rows = await conn.fetch(
        "select id, payload from alerts where user_id = $1 and type = 'breach' and not (payload ? 'breach_key')",
        user_id,
    )
    n = 0
    for r in rows:
        inferred = _infer_breach(str((r["payload"] or {}).get("title", "")))
        if inferred is None:
            continue
        rule, subject, key = inferred
        await conn.execute(
            "update alerts set payload = payload || $2 where id = $1",
            r["id"],
            {
                "rule": rule,
                "subject": subject,
                "breach_key": key,
                "suppress_key": f"breach:{key}",
                "adopted_seed": True,
            },
        )
        n += 1
    return n


# ------------------------------------------------------------------ insert / dedupe
async def insert_alert(
    conn: Connection, user_id: UUID, d: AlertDraft, now: datetime
) -> dict[str, Any] | None:
    """Insert unless suppressed (24 h per suppression group; a LIVE breach is never re-raised) or a duplicate
    dedupe key (unique `(user_id, dedupe_key)`). Returns the alert view when created."""
    if d.suppress_key:
        hit = await conn.fetchval(
            "select 1 from alerts where user_id = $1 and payload ->> 'suppress_key' = $2 "
            "and (created_at > $3 or ($4 and state in ('new', 'opened', 'snoozed'))) limit 1",
            user_id, d.suppress_key, now - SUPPRESSION, d.type in LIVE_REPEAT_TYPES,
        )  # fmt: skip
        if hit:
            return None
    row = await conn.fetchrow(
        "insert into alerts (user_id, symbol, type, materiality, critical, payload, dedupe_key, state, created_at, "
        "delivered_at) values ($1, $2, $3, $4, $5, $6, $7, 'new', $8, $8) "
        "on conflict (user_id, dedupe_key) do nothing returning *",
        user_id, d.symbol, d.type, d.materiality, d.critical, d.to_payload(), d.dedupe_key, now,
    )  # fmt: skip
    if row is None:
        return None
    await write_audit(
        conn,
        user_id=user_id,
        action="alert.create",
        entity="alert",
        entity_id=str(row["id"]),
        details={
            "type": d.type,
            "symbol": d.symbol,
            "critical": d.critical,
            "materiality": d.materiality,
            "dedupe_key": d.dedupe_key,
            "detector": d.payload.get("engine") or d.type,
        },
    )
    return alert_view(row)


async def resolve_cleared_breaches(
    conn: Connection, user_id: UUID, current_keys: set[str], now: datetime
) -> int:
    rows = await conn.fetch(
        "select id, payload from alerts where user_id = $1 and type = 'breach' and state in ('new', 'opened', 'snoozed') "
        "and payload ? 'breach_key'",
        user_id,
    )
    n = 0
    for r in rows:
        if r["payload"]["breach_key"] in current_keys:
            continue
        await conn.execute(
            "update alerts set state = 'dismissed', snoozed_until = null, "
            "payload = payload || jsonb_build_object('resolved', true, 'resolved_at', $2::text) where id = $1",
            r["id"], now.isoformat(),
        )  # fmt: skip
        n += 1
    return n


# ------------------------------------------------------------------ data for the detectors
async def _history(
    conn: Connection, symbols: Sequence[str], today: date
) -> dict[str, list[tuple[date, float]]]:
    rows = await conn.fetch(
        "select symbol, date, close from prices_daily where symbol = any($1::text[]) and date < $2 and date >= $3 "
        "order by symbol, date",
        list(symbols), today, today - timedelta(days=HISTORY_DAYS),
    )  # fmt: skip
    out: dict[str, list[tuple[date, float]]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append((r["date"], float(r["close"])))
    return out


def _past_returns(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    return [
        (series[i][0], series[i][1] / series[i - 1][1] - 1.0)
        for i in range(1, len(series))
        if series[i - 1][1] > 0
    ]


async def evaluate_user(conn: Connection, user_id: UUID, ec: EvalContext) -> EvalResult:
    res = EvalResult()
    now = ec.now
    await unsnooze_due(conn, user_id, now)
    await adopt_seed_breaches(conn, user_id)
    try:
        state = await load_state(conn, user_id, ec.ctx)
    except AppError:  # no paper portfolio yet
        res.skip("no_portfolio")
        return res
    eq_rows = await conn.fetch(
        "select equity from equity_snapshots where portfolio_id = $1 order by ts desc limit 253",
        state.portfolio_id,
    )
    curve = [r["equity"] for r in reversed(eq_rows)]
    risk_pf = state.to_risk()
    metrics = compute_metrics(risk_pf, equity_curve=curve or None)
    thr = await load_threshold(conn, user_id)
    k = float(thr["k"])
    res.k = k
    floor = float(thr["min_materiality"])

    held = {p.symbol: p for p in state.positions}
    equity = state.equity
    weights_pct = {s: float(p.value / equity * 100) for s, p in held.items()} if equity > 0 else {}
    watch = {
        r["symbol"]
        for r in await conn.fetch("select symbol from watchlist_items where user_id = $1", user_id)
    }
    symbols = sorted(set(held) | watch)
    info = {
        r["symbol"]: r
        for r in await conn.fetch(
            "select symbol, name, sector from tickers where symbol = any($1::text[])", symbols
        )
    }
    names = {s: str(r["name"]) for s, r in info.items()}

    drafts: list[AlertDraft] = []
    breaches = detect_breaches(metrics, state.rules, risk_pf.positions, now)
    drafts += breaches
    drafts += detect_concentration(metrics, now)
    drafts += detect_filings(ec.signals, names=names, weights_pct=weights_pct, watch=watch, now=now)

    today = now.date()
    if ec.earnings is not None:
        for sym in symbols:
            found = await ec.earnings.next_report(conn, sym, today)
            if found is None:
                continue
            p = held.get(sym)
            d = detect_earnings(
                symbol=sym, name=names.get(sym, sym), report_date=found[0], today=today,
                qty=float(p.qty) if p else None, weight_pct=weights_pct.get(sym), estimated=found[1], now=now,
            )  # fmt: skip
            if d is not None:
                drafts.append(d)

    hist = await _history(conn, symbols, today)
    quotes = await quotes_for(ec.ctx.prices, symbols, conn)
    for sym in symbols:
        q = quotes.get(sym)
        series = hist.get(sym, [])
        if q is None:
            res.skip("no_quote")
            continue
        allowed = q.freshness in ALLOWED_FRESHNESS
        if ec.move_allowed is not None:
            allowed = allowed and bool(ec.move_allowed(sym))
        if not allowed:
            res.skip("move_suppressed_stale_or_closed")
            continue
        prev = q.prev_close if q.prev_close is not None else (Decimal(str(series[-1][1])) if series else None)
        sigma = daily_sigma([c for _, c in series])
        ret = float(q.price / prev - 1) if prev else 0.0
        note = largest_move_note(ret, _past_returns(series)[-252:], today) if prev else None
        d = detect_move(
            symbol=sym, price=float(q.price), prev_close=float(prev) if prev else None, sigma=sigma, k=k,
            quote_allows=True, weight_pct=weights_pct.get(sym), held=sym in held, now=now, source=q.source,
            freshness=q.freshness, history_note=note,
        )  # fmt: skip
        if d is not None:
            drafts.append(d)

    res.drafts = len(drafts)
    for d in drafts:
        if not d.critical and d.materiality < floor:
            res.skip("below_delivery_floor")
            continue
        view = await insert_alert(conn, user_id, d, now)
        if view is None:
            res.skip("duplicate_or_suppressed")
            continue
        res.created.append(view)
    res.resolved = await resolve_cleared_breaches(
        conn, user_id, {str(b.payload["breach_key"]) for b in breaches}, now
    )
    return res


def Decimal_or_none(x: float) -> Any:
    from decimal import Decimal

    return Decimal(str(x))


# ------------------------------------------------------------------ actions
async def apply_action(
    conn: Connection, user_id: UUID, alert_id: UUID, action: str, now: datetime
) -> dict[str, Any]:
    row = await get_alert(conn, user_id, alert_id, lock=True)
    if row is None:
        raise not_found("Alert not found")
    prev: str = row["state"]
    critical = bool(row["critical"])
    if critical and action in ("dismiss", "snooze"):
        raise conflict(
            "Rule breaches stay pinned: they cannot be dismissed or snoozed. Fix the breach or review your rules.",
            details={"alert_id": str(alert_id), "critical": True, "action": action},
        )
    new_state, until, reward = prev, row["snoozed_until"], None
    if action == "open":
        new_state, until = "opened", None
        reward = bandit.REWARD_OPEN if prev == "new" else None
    elif action == "dismiss" and prev != "dismissed":
        new_state, until, reward = "dismissed", None, bandit.REWARD_DISMISS
    elif action == "snooze" and prev != "snoozed":
        new_state, until, reward = "snoozed", now + SNOOZE, bandit.REWARD_SNOOZE
    elif action == "restore" and prev in ("dismissed", "snoozed"):
        new_state, until = "new", None
    changed = new_state != prev
    if changed or until != row["snoozed_until"]:
        row = await conn.fetchrow(
            "update alerts set state = $2, snoozed_until = $3 where id = $1 returning *",
            alert_id,
            new_state,
            until,
        )
        assert row is not None
    if changed:
        await write_audit(
            conn, user_id=user_id, action=f"alert.{action}", entity="alert", entity_id=str(alert_id),
            details={"from": prev, "to": new_state, "type": row["type"], "symbol": row["symbol"]},
        )  # fmt: skip
    if not critical:
        if reward is not None and changed:
            sector = await bandit.sector_of(conn, row["symbol"])
            await bandit.apply_reward(
                conn, user_id, bandit.arm_for_alert_type(row["type"]), reward, sector=sector, now=now
            )
        if changed and action in ("open", "dismiss", "snooze"):
            await adapt_threshold(conn, user_id, now)
    return alert_view(row)
