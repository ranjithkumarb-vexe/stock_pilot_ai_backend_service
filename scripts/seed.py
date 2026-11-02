"""`make seed`: supported universe + SIMULATED price history + demo personas (BE-04).

    python -m scripts.seed                 # idempotent: reference data is upserted, a persona that already
                                           # has a portfolio is left untouched
    python -m scripts.seed --reset         # rebuild the personas from scratch (deletes their rows first)
    python -m scripts.seed --personas priya,alex

What is written (all through the owner connection, i.e. RLS bypassed - this is an offline job):
  * `tickers`: 16 design large caps + V (17 in the universe) and SPY (benchmark, not tradeable);
  * `prices_daily`: 120 simulated daily bars per symbol, `source = 'simulated'`, last close == design price;
  * per persona (priya, alex, sam): user, profile, rules, watchlist, sector prefs, paper portfolio built from
    REAL fills + cash_ledger rows (average-cost positions identical to the design's holdings and cash),
    120 equity snapshots calibrated to the design stats, events, features, interest vector, bandit arms,
    memories (no embeddings), alerts, and finally rule-breach / review proposals from the risk engine.
Persona ids/emails are `app/core/personas.py`; every id derived here is deterministic (uuid5).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

import asyncpg
import numpy as np

from app.core.config import get_settings
from app.core.db import init_connection
from app.core.personas import PERSONA_NAMESPACE, PERSONAS
from app.modules.paper.ledger import (
    FillRecord,
    LedgerError,
    cash_effect,
    q4,
    rebuild_positions,
    solve_opening_lot,
)
from scripts.seed_data import (
    BENCHMARK,
    PERSONA_SEEDS,
    TIER_RULES,
    UNIVERSE,
    SeedPersona,
    interest_dim,
)
from scripts.seed_series import (
    CurveTarget,
    Session,
    calibrated_equity_curve,
    generate_prices,
    trading_sessions,
)

Conn = Any
SEED_SOURCE = "simulated"


@dataclass
class SeedReport:
    tickers: int = 0
    price_rows: int = 0
    personas: dict[str, str] = field(default_factory=dict)  # key -> seeded | skipped
    proposals: dict[str, int] = field(default_factory=dict)
    alerts: dict[str, int] = field(default_factory=dict)  # alerts the engine ADDED to the design's set
    as_of: str = ""


def _uid(persona: str, kind: str, n: int | str) -> UUID:
    return uuid5(PERSONA_NAMESPACE, f"seed:{persona}:{kind}:{n}")


# --------------------------------------------------------------------------------------------------
# reference data
# --------------------------------------------------------------------------------------------------


async def seed_reference(conn: Conn, sessions: list[Session]) -> tuple[int, int, dict[str, list[Any]]]:
    rows = [
        (t.symbol, t.cik, t.name, t.sector, t.industry, t.exchange, t.in_universe)
        for t in (*UNIVERSE, BENCHMARK)
    ]
    await conn.executemany(
        """
        insert into tickers (symbol, cik, name, sector, industry, exchange, in_universe)
        values ($1, $2, $3, $4, $5, $6, $7)
        on conflict (symbol) do update set
            cik = excluded.cik, name = excluded.name, sector = excluded.sector,
            industry = excluded.industry, exchange = excluded.exchange, in_universe = excluded.in_universe
        """,
        rows,
    )
    bars = generate_prices(list(UNIVERSE), BENCHMARK, sessions)
    symbols = list(bars)
    # replace only rows this seed owns; vendor rows (source != 'simulated') are never touched
    await conn.execute(
        "delete from prices_daily where source = $1 and symbol = any($2::text[])", SEED_SOURCE, symbols
    )
    price_rows = [
        (sym, b.day, b.open, b.high, b.low, b.close, b.close, b.volume, SEED_SOURCE)
        for sym, series in bars.items()
        for b in series
    ]
    await conn.executemany(
        """
        insert into prices_daily (symbol, date, open, high, low, close, adj_close, volume, source)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        on conflict (symbol, date) do nothing
        """,
        price_rows,
    )
    return len(rows), len(price_rows), bars


# --------------------------------------------------------------------------------------------------
# personas
# --------------------------------------------------------------------------------------------------


def _session_near(sessions: list[Session], when: datetime, now: datetime) -> Session:
    """The session a design order "N days ago" lands on: the first one on/after the target that has already
    started, else the last one before it (so a weekend target never ages an order out of its 7-day window)."""
    fwd = [s for s in sessions if s.open_utc >= when and s.open_utc + timedelta(hours=3, minutes=30) <= now]
    if fwd:
        return fwd[0]
    back = [s for s in sessions if s.open_utc <= when]
    return back[-1] if back else sessions[0]


@dataclass(frozen=True)
class PlannedFill:
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    at: datetime
    source: str


def plan_fills(p: SeedPersona, sessions: list[Session], now: datetime) -> list[PlannedFill]:
    """Recent design orders + one opening buy per holding, sized so the replayed positions equal the design."""
    recent: list[PlannedFill] = []
    for idx, o in enumerate(p.orders):
        sess = _session_near(sessions, now - timedelta(days=o.days_ago), now)
        recent.append(
            PlannedFill(
                o.symbol,
                o.side,
                Decimal(o.qty),
                o.price,
                sess.open_utc + timedelta(hours=3, minutes=idx),
                "seed",
            )
        )
    recent.sort(key=lambda f: f.at)
    opening_at = sessions[1].open_utc + timedelta(minutes=5)
    fills: list[PlannedFill] = []
    for k, (sym, qty, avg) in enumerate(p.holdings):
        later = [(f.side, f.qty, f.price) for f in recent if f.symbol == sym]
        try:
            q0, p0 = solve_opening_lot(later, Decimal(qty), avg)  # type: ignore[arg-type]
        except LedgerError as exc:  # pragma: no cover - design data is solvable (tested)
            raise RuntimeError(f"cannot reproduce design holding {sym}: {exc}") from exc
        fills.append(PlannedFill(sym, "buy", q0, p0, opening_at + timedelta(seconds=k), "seed-opening"))
    return fills + recent


async def _reset_persona(conn: Conn, user_id: UUID) -> None:
    await conn.execute("delete from users where id = $1", user_id)


async def seed_persona(
    conn: Conn,
    p: SeedPersona,
    *,
    sessions: list[Session],
    bars: dict[str, list[Any]],
    now: datetime,
    reset: bool,
) -> str:
    ident = PERSONAS[p.key]
    uid = ident.user_id
    if await conn.fetchval("select 1 from portfolios where user_id = $1", uid) and not reset:
        return "skipped"
    # not seeded yet (or --reset): start from a clean slate. A persona row may already exist from dev-login or
    # from other jobs (learned features, events ...); the cascade removes all of it, dev-login re-creates on demand.
    await _reset_persona(conn, uid)

    first = sessions[0]
    last = sessions[-1]
    await conn.execute(
        """
        insert into users (id, email, display_name, persona_id, onboarded_at, created_at)
        values ($1, $2, $3, $4, $5, $5)
        on conflict (id) do update set
            display_name = excluded.display_name, persona_id = excluded.persona_id,
            onboarded_at = coalesce(users.onboarded_at, excluded.onboarded_at)
        """,
        uid, ident.email, ident.display_name, p.key, first.open_utc,
    )  # fmt: skip

    ack_at = first.open_utc
    await conn.execute(
        """
        insert into profiles (user_id, risk_tier_stated, horizon, goals, experience, region, currency,
                              esg_exclusions, disclaimer_ack_at, created_at, updated_at)
        values ($1, $2, $3, $4, $5, 'US', 'USD', $6, $7, $7, $7)
        """,
        uid, p.tier, p.horizon, list(p.goals), p.experience, list(p.excluded), ack_at,
    )  # fmt: skip
    await conn.execute(
        "insert into audit_log (user_id, ts, action, entity, entity_id, details) values ($1, $2, 'disclaimer.ack', 'profile', $3, $4)",
        uid, ack_at, str(uid), {"source": "seed"},
    )  # fmt: skip
    defaults = TIER_RULES[p.tier]
    for key, val in p.rules.items():
        await conn.execute(
            "insert into risk_rules (user_id, rule_key, value, source, updated_at) values ($1, $2, $3, $4, $5)",
            uid, key, Decimal(val), "default" if defaults[key] == val else "user", first.open_utc,
        )  # fmt: skip
    for k, sym in enumerate(p.watchlist):
        await conn.execute(
            "insert into watchlist_items (user_id, symbol, added_at) values ($1, $2, $3)",
            uid, sym, first.open_utc + timedelta(seconds=k),
        )  # fmt: skip
    for sector in p.likes:
        await conn.execute("insert into sector_prefs (user_id, sector, pref) values ($1, $2, 1)", uid, sector)

    # ---- paper portfolio from real fills ------------------------------------------------------
    plan = plan_fills(p, sessions, now)
    net_spend = sum((-cash_effect(f.side, f.qty, f.price) for f in plan), Decimal(0))  # type: ignore[arg-type]
    starting_cash = (p.cash + net_spend).quantize(Decimal("0.01"))
    pid = _uid(p.key, "portfolio", 1)
    await conn.execute(
        "insert into portfolios (id, user_id, name, starting_cash, created_at) values ($1, $2, 'Main', $3, $4)",
        pid, uid, starting_cash, first.open_utc,
    )  # fmt: skip
    await conn.execute(
        "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount, ref_id) values ($1, $2, $3, 'deposit', $4, null)",
        pid, uid, first.open_utc, starting_cash,
    )  # fmt: skip
    rounding = (p.cash + net_spend) - starting_cash  # sub-cent residual: starting_cash is a 2 dp column
    if rounding != 0:
        await conn.execute(
            "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount, ref_id) values ($1, $2, $3, 'adjustment', $4, null)",
            pid, uid, first.open_utc, rounding,
        )  # fmt: skip
    records: list[FillRecord] = []
    for n, f in enumerate(plan):
        oid, fid = _uid(p.key, "order", n), _uid(p.key, "fill", n)
        await conn.execute(
            """
            insert into orders (id, portfolio_id, user_id, symbol, side, type, qty, tif, status, idempotency_key,
                                source, checks, created_at, updated_at, expires_at)
            values ($1, $2, $3, $4, $5, 'market', $6, 'day', 'filled', $7, $8, '[]'::jsonb, $9, $9, $9)
            """,
            oid, pid, uid, f.symbol, f.side, f.qty, f"seed-{p.key}-{n}", f.source, f.at,
        )  # fmt: skip
        await conn.execute(
            "insert into fills (id, order_id, user_id, portfolio_id, symbol, qty, price, slippage_bps, fee, filled_at) "
            "values ($1, $2, $3, $4, $5, $6, $7, 0, 0, $8)",
            fid, oid, uid, pid, f.symbol, f.qty, f.price, f.at,
        )  # fmt: skip
        await conn.execute(
            "insert into cash_ledger (portfolio_id, user_id, ts, kind, amount, ref_id) values ($1, $2, $3, 'fill', $4, $5)",
            pid, uid, f.at, cash_effect(f.side, f.qty, f.price), fid,  # type: ignore[arg-type]
        )  # fmt: skip
        records.append(FillRecord(f.symbol, f.side, f.qty, f.price, f.at))  # type: ignore[arg-type]
    book = rebuild_positions(records)
    for sym, lot in book.items():
        await conn.execute(
            "insert into positions (portfolio_id, symbol, user_id, qty, avg_cost, updated_at) values ($1, $2, $3, $4, $5, $6)",
            pid, sym, uid, lot.qty, lot.avg_cost, now,
        )  # fmt: skip
    expect = {s: (Decimal(q), q4(c)) for s, q, c in p.holdings}
    got = {s: (lot.qty, lot.avg_cost) for s, lot in book.items()}
    if got != expect:
        raise RuntimeError(f"{p.key}: replayed positions {got} differ from design {expect}")
    cash = starting_cash + rounding + sum((cash_effect(f.side, f.qty, f.price) for f in plan), Decimal(0))  # type: ignore[arg-type]
    if cash != p.cash:
        raise RuntimeError(f"{p.key}: cash {cash} differs from design {p.cash}")

    # ---- equity snapshots (calibrated to the design stats; last point == real current equity) -------
    last_px = {sym: bars[sym][-1].close for sym in bars}
    equity_now = cash + sum((lot.qty * last_px[sym] for sym, lot in book.items()), Decimal(0))
    spy = np.array([float(b.close) for b in bars[BENCHMARK.symbol]])
    if p.holdings:
        s = p.stats
        curve = calibrated_equity_curve(
            spy, CurveTarget(s["ytd"], s["vol"], s["beta"], s["dd"], s["maxdd"]), float(equity_now), p.key
        )
    else:
        curve = np.full(len(sessions), float(equity_now))
    first_equity = float(curve[0])
    flows = sorted(((f.at, cash_effect(f.side, f.qty, f.price)) for f in plan), key=lambda x: x[0])  # type: ignore[arg-type]
    cash_rows: list[Decimal] = []
    running, k = starting_cash + rounding, 0
    for sess in sessions:
        while k < len(flows) and flows[k][0] <= sess.close_utc:
            running += flows[k][1]
            k += 1
        cash_rows.append(running)
    for i, sess in enumerate(sessions):
        eq = Decimal(f"{curve[i]:.2f}") if i < len(sessions) - 1 else equity_now.quantize(Decimal("0.01"))
        spy_norm = Decimal(f"{first_equity * spy[i] / spy[0]:.6f}")
        await conn.execute(
            "insert into equity_snapshots (portfolio_id, user_id, ts, equity, cash, spy_equity_norm) values ($1,$2,$3,$4,$5,$6)",
            pid, uid, sess.close_utc, eq, cash_rows[i].quantize(Decimal("0.01")), spy_norm,
        )  # fmt: skip

    await _seed_personalization(conn, p, uid, last, now, plan)
    await _seed_alerts(conn, p, uid, now)
    await conn.execute(
        "insert into audit_log (user_id, ts, action, entity, entity_id, details) values ($1, $2, 'seed.persona', 'user', $3, $4)",
        uid, now, str(uid), {"persona": p.key, "source": "scripts.seed"},
    )  # fmt: skip
    return "seeded"


async def _seed_personalization(
    conn: Conn, p: SeedPersona, uid: UUID, last: Session, now: datetime, plan: list[PlannedFill]
) -> None:
    # interest vector (baseline == initial weight) and bandit arms
    for name, w in p.interest:
        await conn.execute(
            "insert into user_interest_vector (user_id, dim, weight, baseline, updated_at) values ($1, $2, $3, $3, $4)",
            uid, interest_dim(name), Decimal(str(w)), now,
        )  # fmt: skip
    for arm, (a, b) in p.arms.items():
        await conn.execute(
            "insert into bandit_arms (user_id, arm, context, alpha, beta, updated_at) values ($1, $2, '', $3, $4, $5)",
            uid, arm, Decimal(a), Decimal(b), now,
        )  # fmt: skip
    for n, m in enumerate(p.memories):
        await conn.execute(
            "insert into memories (id, user_id, kind, text, source, confidence, created_at) values ($1, $2, $3, $4, $5, $6, $7)",
            _uid(p.key, "memory", str(m["id"])), uid, m["kind"], m["text"], m["src"], Decimal(str(m["conf"])), now - timedelta(days=3) + timedelta(minutes=n),
        )  # fmt: skip

    # features read by GET /profile (stream C's recompute_features job may overwrite them)
    style = {
        "depth": p.depth, "jargon": p.jargon, "tone": p.tone, "metric": p.metric, "language": p.language,
        "section_order": list(p.section_order), "length": "medium", "quant_vs_narrative": 0.5,
    }  # fmt: skip
    style["order"] = list(
        p.section_order
    )  # the key the brief generator reads (CR-22); same list as `section_order`
    feats: list[tuple[str, dict[str, Any], float, int | None]] = [
        ("style_card", style, 0.8, None),
        ("revealed_tier", {"value": p.revealed_tier}, 0.7, 30),
        ("gap_days", {"value": p.gap_days}, 0.7, 30),
    ]
    for name, metric, value, state in p.bias:
        slug = {"Overtrading": "overtrading", "Disposition effect": "disposition", "Concentration": "concentration",
                "Herding": "herding", "Loss aversion": "loss_aversion"}[name]  # fmt: skip
        if state == "calc":
            continue  # concentration is computed live from the portfolio (HHI)
        feats.append(
            (f"bias:{slug}", {"name": name, "metric": metric, "value": value, "state": state}, 0.7, 30)
        )
    for key, fvalue, conf, half in feats:
        await conn.execute(
            "insert into user_features (user_id, key, value, confidence, half_life_days, updated_at) values ($1, $2, $3, $4, $5, $6)",
            uid, key, fvalue, Decimal(str(conf)), half, now,
        )  # fmt: skip

    # events: the design's activity log + the trade/decision history the stats were computed from
    ev: list[tuple[str, datetime, str, dict[str, Any], dict[str, Any]]] = []
    w0 = p.watchlist[0]
    ev.append(("ticker_viewed", now - timedelta(hours=3), "ticker_viewed", {"ticker": w0}, {}))
    ev.append(
        (
            "section_expanded",
            now - timedelta(hours=3, minutes=-2),
            "section_expanded",
            {"ticker": w0, "section": p.section_order[0]},
            {"dwell_ms": 22000},
        )
    )
    ev.append(("alert_opened", now - timedelta(days=1), "alert_opened", {"ticker": w0}, {"action": "open"}))
    for label, sym, reason in p.rejections:
        ev.append(
            (
                f"reject-{label}",
                now - timedelta(days=9),
                "proposal_rejected",
                {"ticker": sym, "proposal_ref": label},
                {"reason": reason},
            )
        )
    for n, f in enumerate(plan):
        ev.append(
            (
                f"fill-{n}",
                f.at,
                "order_filled",
                {"ticker": f.symbol},
                {"side": f.side, "qty": int(f.qty), "price": float(f.price), "source": f.source},
            )
        )
    ev.append(("onboarding", last.open_utc, "onboarding_complete", {}, {"tier": p.tier}))
    for i, (key, ts, typ, entity, props) in enumerate(ev):
        await conn.execute(
            """
            insert into events (event_id, user_id, ts, session_id, type, entity, props, context, client, schema_version)
            values ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb, '{"device": "seed", "tz": "UTC"}'::jsonb, 1)
            """,
            _uid(p.key, "event", f"{i}:{key}"), uid, ts, f"seed-{p.key}", typ, entity, props,
        )  # fmt: skip


async def _seed_alerts(conn: Conn, p: SeedPersona, uid: UUID, now: datetime) -> None:
    for a in p.alerts:
        created = now - timedelta(minutes=int(a["age_min"]))  # type: ignore[call-overload]
        payload = {"title": a["title"], "body": a["body"], "source_label": a["src"], "seed_id": a["id"]}
        await conn.execute(
            """
            insert into alerts (id, user_id, symbol, type, materiality, critical, payload, dedupe_key, state, created_at, delivered_at)
            values ($1, $2, $3, $4, $5, $6, $7, $8, 'new', $9, $9)
            """,
            _uid(p.key, "alert", str(a["id"])), uid, a["symbol"], a["type"], Decimal(str(a["mat"])), bool(a["crit"]),
            payload, f"seed:{p.key}:{a['id']}", created,
        )  # fmt: skip


# --------------------------------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------------------------------


async def seed(
    conn: Conn,
    *,
    now: datetime | None = None,
    reset: bool = False,
    personas: list[str] | None = None,
    refresh_proposals: bool = True,
    refresh_alerts: bool = True,
) -> SeedReport:
    """Seed inside the caller's connection. Wrapped in one transaction so a failure leaves nothing half-done."""
    now = now or datetime.now(UTC)
    keys = list(PERSONA_SEEDS) if personas is None else personas  # [] = universe + prices only (production)
    unknown = set(keys) - set(PERSONA_SEEDS)
    if unknown:
        raise ValueError(f"unknown persona(s): {sorted(unknown)}")
    sessions = trading_sessions(now)
    report = SeedReport(as_of=sessions[-1].day.isoformat())
    async with conn.transaction():
        report.tickers, report.price_rows, bars = await seed_reference(conn, sessions)
        for key in keys:
            report.personas[key] = await seed_persona(
                conn, PERSONA_SEEDS[key], sessions=sessions, bars=bars, now=now, reset=reset
            )
        if refresh_proposals:
            from app.modules.proposals.seedhook import refresh_for_seed

            for key in keys:
                if report.personas[key] == "seeded":
                    report.proposals[key] = await refresh_for_seed(conn, PERSONAS[key].user_id, now)
        if refresh_alerts:
            # adopts the design's breach alerts with the risk engine's breach keys (no duplicates later) (CR-22)
            from app.modules.alerts.seedhook import refresh_for_seed as refresh_alerts_for_seed

            for key in keys:
                if report.personas[key] == "seeded":
                    report.alerts[key] = await refresh_alerts_for_seed(conn, PERSONAS[key].user_id, now)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Seed the StockPilot universe and demo personas")
    ap.add_argument("--reset", action="store_true", help="rebuild personas from scratch")
    ap.add_argument(
        "--personas",
        default="",
        help="comma separated subset of priya,alex,sam (default: all three; 'none' = no personas)",
    )
    ap.add_argument(
        "--universe-only",
        action="store_true",
        help="production: tickers + simulated daily prices only, no demo personas, proposals or alerts",
    )
    return ap.parse_args(argv)


def persona_keys(args: argparse.Namespace) -> list[str] | None:
    """None = all personas; [] = none (`--universe-only`, `--personas none`). Contradictory flags are an error."""
    names = [k.strip() for k in args.personas.split(",") if k.strip()]
    wants_none = args.universe_only or [n.lower() for n in names] == ["none"]
    if wants_none:
        if args.universe_only and names and [n.lower() for n in names] != ["none"]:
            raise SystemExit("--universe-only cannot be combined with --personas " + args.personas)
        if args.reset:
            raise SystemExit("--reset rebuilds personas; it cannot be combined with --universe-only / none")
        return []
    return names or None


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    keys = persona_keys(args)
    settings = get_settings()
    conn = await asyncpg.connect(settings.database_admin_url)
    await init_connection(conn)
    try:
        report = await seed(conn, reset=args.reset, personas=keys)
    finally:
        await conn.close()
    print(
        f"seed ok (prices through {report.as_of}, source={SEED_SOURCE}): {report.tickers} tickers, "
        f"{report.price_rows} price rows; personas: "
        + (", ".join(f"{k}={v}" for k, v in report.personas.items()) or "none (universe only)")
        + (
            "; proposals: " + ", ".join(f"{k}={v}" for k, v in report.proposals.items())
            if report.proposals
            else ""
        )
        + (
            "; alerts added by the engine: " + ", ".join(f"{k}={v}" for k, v in report.alerts.items())
            if any(report.alerts.values())
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
