"""`recompute_features(user_id)`: rebuild every learned feature of one user from their own data (SPEC 10.5).

Read-only against the profile and trading tables (positions, fills, orders, proposals, prices): the stated risk
tier is never written (T-PERS-04). Writes only `user_features`, `user_interest_vector` and auto-derived
`memories`. Runs inside an RLS transaction for that user (pass `conn`, or it opens `user_tx`).
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.db import Connection, user_tx
from app.core.logging import get_logger
from app.modules.personalization import bias as bias_mod
from app.modules.personalization import features as feat
from app.modules.personalization import memory as mem
from app.modules.personalization.revealed_risk import compute_revealed_risk, gap_state
from app.modules.research.brief.style import default_style_from_profile  # stream B's cold-start order (CR-5)

log = get_logger("app.personalization.recompute")

EVENT_WINDOW = timedelta(days=90)
FILL_WINDOW = timedelta(days=365)
PRICE_WINDOW = timedelta(days=420)
BENCHMARK = "SPY"
BIAS_ORDER = bias_mod.SLUGS


@dataclass(slots=True)
class RecomputeResult:
    user_id: UUID
    n_events: int
    revealed_tier: float | None
    gap_days: int
    bias: list[dict[str, Any]] = field(default_factory=list)
    memories: dict[str, int] = field(default_factory=dict)


class PriceBook:
    """Daily closes per symbol with nearest-prior lookups."""

    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self.closes: dict[str, dict[date, float]] = defaultdict(dict)
        self.bars: dict[str, list[tuple[date, float, float | None]]] = defaultdict(list)
        for r in rows:
            close = float(r["close"])
            self.closes[r["symbol"]][r["date"]] = close
            self.bars[r["symbol"]].append(
                (r["date"], close, None if r["volume"] is None else float(r["volume"]))
            )
        self._days = {s: sorted(c) for s, c in self.closes.items()}

    def close_on(self, symbol: str, day: date, max_gap_days: int = 5) -> float | None:
        days = self._days.get(symbol)
        if not days:
            return None
        i = bisect.bisect_right(days, day)
        if i == 0:
            return None
        found = days[i - 1]
        return self.closes[symbol][found] if (day - found).days <= max_gap_days else None

    def last_close(self, symbol: str) -> float | None:
        days = self._days.get(symbol)
        return self.closes[symbol][days[-1]] if days else None


async def _put(
    conn: Connection,
    user_id: UUID,
    key: str,
    value: Any,
    confidence: float,
    half_life: int | None,
    now: datetime,
) -> None:
    await conn.execute(
        "insert into user_features (user_id, key, value, confidence, half_life_days, updated_at) "
        "values ($1, $2, $3, $4, $5, $6) on conflict (user_id, key) do update set value = excluded.value, "
        "confidence = excluded.confidence, half_life_days = excluded.half_life_days, updated_at = excluded.updated_at",
        user_id, key, value, round(max(0.0, min(1.0, confidence)), 4), half_life, now,
    )  # fmt: skip


async def recompute_features(
    user_id: UUID, *, now: datetime, conn: Connection | None = None
) -> RecomputeResult:
    if conn is not None:
        return await _recompute(conn, user_id, now)
    async with user_tx(user_id) as c:
        return await _recompute(c, user_id, now)


async def _recompute(conn: Connection, user_id: UUID, now: datetime) -> RecomputeResult:
    event_rows = await conn.fetch(
        "select event_id, ts, type, entity, props from events where user_id = $1 and ts >= $2 and ts <= $3 order by ts",
        user_id, now - EVENT_WINDOW, now,
    )  # fmt: skip
    events = [
        feat.EventRow(str(r["event_id"]), r["ts"], r["type"], r["entity"] or {}, r["props"] or {})
        for r in event_rows
    ]
    sector_of = {
        r["symbol"]: r["sector"]
        for r in await conn.fetch("select symbol, sector from tickers")
        if r["sector"]
    }
    profile = await conn.fetchrow(
        "select risk_tier_stated, goals, experience from profiles where user_id = $1", user_id
    )
    existing = {
        r["key"]: r["value"]
        for r in await conn.fetch("select key, value from user_features where user_id = $1", user_id)
    }
    base_rows = await conn.fetch(
        "select dim, weight, baseline from user_interest_vector where user_id = $1", user_id
    )
    baseline = {r["dim"]: float(r["baseline"]) for r in base_rows}
    n = len(events)
    evidence_conf = min(0.95, 0.3 + n / 100.0)

    # --- interest vector, drift, topics, sections, pinned metrics, style card --------------------------
    contrib = feat.interest_contributions(events, sector_of, now)
    interest = feat.merge_interest(baseline, contrib)
    for dim, w in interest.items():
        await conn.execute(
            "insert into user_interest_vector (user_id, dim, weight, baseline, updated_at) values ($1, $2, $3, 0, $4) "
            "on conflict (user_id, dim) do update set weight = excluded.weight, updated_at = excluded.updated_at",
            user_id, dim, w, now,
        )  # fmt: skip
    betas = feat.section_beta(events, now)
    pinned = feat.pinned_metrics(events, now)
    topics = feat.topic_histogram(events, now)
    style = feat.derive_style_card(
        existing=existing.get("style_card") if isinstance(existing.get("style_card"), dict) else None,
        experience=profile["experience"] if profile else None,
        goals=list(profile["goals"]) if profile and profile["goals"] else [],
        events=events,
        betas=betas,
        pinned=pinned,
        now=now,
        default_order=default_style_from_profile(
            profile["experience"] if profile else None, list(profile["goals"] or []) if profile else []
        ).order,
    )
    await _put(conn, user_id, "style_card", style, evidence_conf, 14, now)
    await _put(conn, user_id, "section_beta", betas, evidence_conf, 14, now)
    await _put(conn, user_id, "pinned_metrics", {"metrics": pinned}, 0.8 if pinned else 0.3, 7, now)
    await _put(conn, user_id, "topic_histogram", topics, evidence_conf, 14, now)
    await _put(
        conn, user_id, "interest_drift", feat.interest_drift(events, sector_of, now), evidence_conf, 28, now
    )

    # --- portfolio-derived: revealed risk, gap, bias ----------------------------------------------------
    positions = await conn.fetch(
        "select symbol, qty, avg_cost from positions where user_id = $1 and qty > 0", user_id
    )
    fill_rows = await conn.fetch(
        "select f.filled_at, f.symbol, o.side, f.qty, f.price from fills f join orders o "
        "on o.id = f.order_id and o.user_id = f.user_id where f.user_id = $1 and f.filled_at >= $2 and f.filled_at <= $3 "
        "order by f.filled_at",
        user_id, now - FILL_WINDOW, now,
    )  # fmt: skip
    trades = [
        bias_mod.Trade(r["filled_at"], r["symbol"], r["side"], float(r["qty"]), float(r["price"]))
        for r in fill_rows
    ]
    symbols = sorted({p["symbol"] for p in positions} | {t.symbol for t in trades} | {BENCHMARK})
    price_rows = await conn.fetch(
        "select symbol, date, close, volume from prices_daily where symbol = any($1::text[]) and date >= $2 order by symbol, date",
        symbols, (now - PRICE_WINDOW).date(),
    )  # fmt: skip
    book = PriceBook(list(price_rows))

    values = {
        p["symbol"]: float(p["qty"]) * (book.last_close(p["symbol"]) or float(p["avg_cost"]))
        for p in positions
    }
    revealed = compute_revealed_risk(values, book.closes, book.closes.get(BENCHMARK))
    stated = int(profile["risk_tier_stated"]) if profile and profile["risk_tier_stated"] is not None else None
    raw_gap = existing.get("gap_days")
    prev_gap: dict[str, Any] = dict(raw_gap) if isinstance(raw_gap, dict) else {}
    prev_since = _parse_date(prev_gap.get("since"))
    gap = gap_state(
        stated=stated,
        revealed=revealed.tier,
        previous_days=int(prev_gap.get("value", 0) or 0),
        previous_since=prev_since,
        today=now.date(),
    )
    if revealed.tier is not None:
        await _put(
            conn, user_id, "revealed_tier",
            {"value": revealed.tier, "vol_pct": revealed.vol_pct, "beta": revealed.beta, "n_obs": revealed.n_obs,
             "stated": stated, "gap": gap["gap"], "gap_flag": gap["flag"], "flags": list(revealed.flags)},
            min(0.95, revealed.n_obs / 60.0), 30, now,
        )  # fmt: skip
    since = gap["since"]
    await _put(
        conn, user_id, "gap_days",
        {"value": gap["gap_days"], "since": since.isoformat() if isinstance(since, date) else None, "flag": gap["flag"]},
        0.7, 30, now,
    )  # fmt: skip

    rules = {
        r["rule_key"]: float(r["value"])
        for r in await conn.fetch("select rule_key, value from risk_rules where user_id = $1", user_id)
    }
    portfolio_value = sum(values.values())
    spikes = {s: bias_mod.spike_dates(book.bars.get(s, [])) for s in {t.symbol for t in trades}}
    decisions = await _reduce_decisions(conn, user_id, positions, book)
    results = [
        bias_mod.detect_overtrading(trades, now=now, rule_limit=int(rules["trades"]) if "trades" in rules else None, portfolio_value=portfolio_value),
        bias_mod.detect_disposition(trades, book.close_on),
        bias_mod.detect_concentration(values),
        bias_mod.detect_herding(trades, spikes),
        bias_mod.detect_loss_aversion(decisions),
    ]  # fmt: skip
    ordered = sorted(results, key=lambda r: BIAS_ORDER.index(r.slug))
    for r in ordered:
        await _put(conn, user_id, f"bias:{r.slug}", r.as_dict(), min(0.9, 0.4 + r.n / 30.0), 30, now)
    bias_list = [r.as_dict() for r in ordered]
    await _put(conn, user_id, "bias_flags", {"items": bias_list}, 0.7, 30, now)

    # --- memories (user's own events only) -----------------------------------------------------------
    cands, contradicted = mem.extract_candidates(
        events=events, interest=interest, baseline=baseline, betas=betas, pinned=pinned
    )
    mem_stats = await mem.consolidate(conn, user_id, cands, contradicted, now)
    await _put(conn, user_id, "features_computed_at", {"at": now.isoformat(), "n_events": n}, 1.0, None, now)
    log.info("features recomputed", extra={"n_events": n, "memories": mem_stats})
    return RecomputeResult(user_id, n, revealed.tier, int(gap["gap_days"]), bias_list, mem_stats)  # type: ignore[call-overload]


async def _reduce_decisions(
    conn: Connection, user_id: UUID, positions: list[Any], book: PriceBook
) -> list[bias_mod.ReduceDecision]:
    """Decided 'reduce exposure' (sell) trade proposals, classified winner/loser by the position's cost basis at
    the time the proposal was made."""
    avg = {p["symbol"]: float(p["avg_cost"]) for p in positions}
    rows = await conn.fetch(
        "select symbol, status, created_at from proposals where user_id = $1 and type = 'trade' "
        "and payload ->> 'side' = 'sell' and status in ('approved', 'rejected', 'modified') and symbol is not null",
        user_id,
    )
    out: list[bias_mod.ReduceDecision] = []
    for r in rows:
        px = book.close_on(r["symbol"], r["created_at"].date(), max_gap_days=10)
        cost = avg.get(r["symbol"])
        if px is None or cost is None:
            continue
        out.append(bias_mod.ReduceDecision(rejected=r["status"] == "rejected", loser=px < cost))
    return out


def _parse_date(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
