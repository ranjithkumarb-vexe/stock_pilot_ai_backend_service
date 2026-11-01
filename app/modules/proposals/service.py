"""Generating, listing and deciding proposals.

Generation is deterministic: the risk engine (never a model) finds rule breaches and review triggers, and the
wording comes from `templates.py` only. Approving a trade proposal executes ONE order through the paper engine
(`paper.service.place_order`, source "proposal"); nothing else can execute a trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from app.core.audit import write_audit
from app.core.db import Connection
from app.core.errors import conflict, not_found, validation_error
from app.modules.paper import service as paper
from app.modules.paper.context import EngineContext
from app.modules.paper.schemas import OrderOut, dump
from app.modules.paper.state import PortfolioState, get_portfolio_row, load_state
from app.modules.proposals import hooks, templates
from app.modules.risk import Portfolio, TradeProposal, compute_metrics, suggest_reduction, validate
from app.modules.tradingcommon.disclaimer import DISCLAIMER_TEXT
from app.modules.tradingcommon.events import emit_event

HUNDRED = Decimal(100)
EXPIRY = timedelta(hours=24)
COOLDOWN = timedelta(hours=24)
DROP_REVIEW_PCT = Decimal(4)  # |one-day fall| at/above which a holding gets a review proposal
PACE_REVIEW_FRACTION = Decimal("0.9")  # of the weekly trade limit
DECIDED = ("approved", "modified", "rejected")
_KEY_NS = UUID("0e3f6a3e-6f0b-4d3e-8a55-6a1d4f2c9b10")


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: str  # trade | review
    rule: str  # pos | sec | drop | overtrading
    symbol: str | None
    side: str | None
    title: str
    body: str
    rationale: list[str]
    rules_checked: list[dict[str, Any]]
    sources: list[str]
    suggested_qty: int
    current_weight_pct: float | None
    after_weight_pct: float | None
    cap_pct: float | None
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "rule": self.rule, "kind": self.kind, "side": self.side, "title": self.title, "body": self.body,
            "sources": self.sources, "suggested_qty": self.suggested_qty,
            "current_weight_pct": self.current_weight_pct, "after_weight_pct": self.after_weight_pct,
            "cap_pct": self.cap_pct, "generator": "risk_engine/1", **self.extra,
        }  # fmt: skip


def _w(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01")))


def _sell_check(state: PortfolioState, symbol: str, qty: int) -> list[dict[str, Any]]:
    """Rules checked for the SUGGESTED sale, by the risk engine."""
    pos = state.position(symbol)
    res = validate(
        TradeProposal(
            symbol, "sell", Decimal(qty), pos.price if pos else Decimal(0), pos.sector if pos else None
        ),
        Portfolio(state.cash, state.to_risk().positions, state.trades_7d),
        state.rules,
    )
    return [{"rule": c.label, "passed": c.ok, "detail": c.detail} for c in res.checks]


def generate_candidates(state: PortfolioState) -> list[Candidate]:
    """All proposals that SHOULD exist for this portfolio right now (deterministic, idempotent)."""
    risk_pf = state.to_risk()
    equity = state.equity
    rules = state.rules
    out: list[Candidate] = []
    if equity <= 0 or not state.positions:
        return out
    metrics = compute_metrics(risk_pf)
    pos_breach: set[str] = set()

    for sym, w in sorted(metrics.weights.items(), key=lambda kv: -kv[1]):
        weight = w * HUNDRED
        if weight <= rules.pos:
            continue
        qty = suggest_reduction(risk_pf, rules, rule="pos", symbol=sym)
        if qty <= 0:
            continue
        p = state.position(sym)
        assert p is not None
        after = (p.value - qty * p.price) / equity * HUNDRED
        cap = templates.fmt_cap(rules.pos)
        fields = {
            "cap": cap,
            "symbol": sym,
            "weight": templates.pct1(weight),
            "over": templates.pct1(weight - rules.pos),
            "qty": qty,
            "after": templates.pct1(after),
        }
        out.append(
            Candidate(
                "trade", "pos", sym, "sell", templates.TITLE_POSITION.format(cap=cap), templates.BODY_POSITION.format(**fields),
                [templates.RATIONALE_POSITION.format(**fields), templates.RATIONALE_ENGINE, templates.RATIONALE_NEVER_LOOSENED],
                _sell_check(state, sym, qty), [templates.SOURCE_RISK_POS], qty, _w(weight), _w(after), float(rules.pos),
                {"subject": sym, "sector": p.sector, "price": float(p.price)},
            )
        )  # fmt: skip
        pos_breach.add(sym)

    for sector, w in sorted(metrics.sector_weights.items(), key=lambda kv: -kv[1]):
        weight = w * HUNDRED
        if weight <= rules.sec:
            continue
        members = [p for p in state.positions if (p.sector or "Unclassified") == sector]
        eligible = [p for p in members if p.symbol not in pos_breach] or members
        # the holding with the smallest unrealised gain (least taxable gain to realise); ties by symbol
        pick = min(eligible, key=lambda p: (p.price / p.avg_cost if p.avg_cost > 0 else Decimal(1), p.symbol))
        qty = suggest_reduction(risk_pf, rules, rule="sec", symbol=pick.symbol)
        if qty <= 0:
            continue
        sector_value = sum((p.value for p in members), Decimal(0))
        after = (sector_value - qty * pick.price) / equity * HUNDRED
        cap = templates.fmt_cap(rules.sec)
        fields = {
            "cap": cap,
            "sector": sector,
            "symbol": pick.symbol,
            "weight": templates.pct1(weight),
            "over": templates.pct1(weight - rules.sec),
            "qty": qty,
            "after": templates.pct1(after),
        }
        out.append(
            Candidate(
                "trade", "sec", pick.symbol, "sell", templates.TITLE_SECTOR.format(sector=sector, cap=cap), templates.BODY_SECTOR.format(**fields),
                [templates.RATIONALE_SECTOR.format(**fields), templates.RATIONALE_SECTOR_PICK.format(symbol=pick.symbol, sector=sector), templates.RATIONALE_ENGINE, templates.RATIONALE_NEVER_LOOSENED],
                _sell_check(state, pick.symbol, qty), [templates.SOURCE_RISK_SEC], qty, _w(weight), _w(after), float(rules.sec),
                {"subject": sector, "sector": sector, "price": float(pick.price)},
            )
        )  # fmt: skip

    for p in sorted(state.positions, key=lambda x: x.symbol):
        if p.change_pct <= -DROP_REVIEW_PCT:
            drop = templates.pct1(-p.change_pct)
            weight = p.value / equity * HUNDRED
            fields = {
                "name": p.name or p.symbol,
                "drop": drop,
                "qty": int(p.qty),
                "weight": templates.pct1(weight),
            }
            out.append(
                Candidate(
                    "review", "drop", p.symbol, None, templates.TITLE_DROP.format(name=p.name or p.symbol, drop=drop), templates.BODY_DROP.format(**fields),
                    [templates.RATIONALE_DROP.format(drop=DROP_REVIEW_PCT), templates.RATIONALE_NO_TRADE], [], [templates.SOURCE_PRICE], 0,
                    _w(weight), None, None, {"subject": p.symbol, "sector": p.sector, "change_pct": float(p.change_pct.quantize(Decimal("0.01")))},
                )
            )  # fmt: skip

    threshold = max(2, math.ceil(Decimal(rules.trades) * PACE_REVIEW_FRACTION))
    if state.trades_7d >= threshold:
        fields = {"n": state.trades_7d, "limit": rules.trades}
        out.append(
            Candidate(
                "review", "overtrading", None, None, templates.TITLE_PACE.format(n=state.trades_7d), templates.BODY_PACE.format(limit=rules.trades),
                [templates.RATIONALE_PACE.format(**fields), templates.RATIONALE_NO_TRADE], [], [templates.SOURCE_PACE, templates.SOURCE_STUDY],
                0, None, None, None, {"subject": "trades_7d", "trades_7d": state.trades_7d},
            )
        )  # fmt: skip

    for c in out:  # defence in depth: nothing that breaks the wording rules is ever stored
        templates.assert_clean(c.title, c.body, *c.rationale, *c.sources)
    return out


# --------------------------------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------------------------------


def _why(p: dict[str, Any]) -> dict[str, Any]:
    kind_rule = p["rule"]
    if p["kind"] == "trade":
        feature = {
            "name": "rule_breach",
            "weight": 1.0,
            "detail": templates.FEATURE_BREACH.format(detail=p["title"]),
        }
        summary = templates.WHY_PINNED
    else:
        label = templates.FEATURE_MOVE if kind_rule == "drop" else templates.FEATURE_PACE
        feature = {"name": "review_trigger", "weight": 0.6, "detail": label.format(detail=p["title"])}
        summary = templates.WHY_REVIEW
    return {
        "summary": summary, "top_features": [feature], "rules_checked": p["rules_checked"], "sources": p["sources"], "basis": "profile",
    }  # fmt: skip


def _num(x: Any) -> int | float | None:
    if x is None:
        return None
    d = Decimal(str(x))
    return int(d) if d == d.to_integral_value() else float(d)


def proposal_view(row: Any, order_id: UUID | None = None) -> dict[str, Any]:
    pl = row["payload"]
    qty = row["modified_qty"] if row["modified_qty"] is not None else pl.get("suggested_qty", 0)
    view = {
        "id": row["id"], "kind": row["type"], "symbol": row["symbol"], "side": pl.get("side"), "rule": pl.get("rule"),
        "title": pl["title"], "body": pl["body"], "rationale": list(row["rationale"]), "rules_checked": list(row["rules_checked"]),
        "suggested_qty": pl.get("suggested_qty", 0), "current_weight_pct": pl.get("current_weight_pct"),
        "after_weight_pct": pl.get("after_weight_pct"), "cap_pct": pl.get("cap_pct"), "sources": pl.get("sources", []),
        "status": row["status"], "expires_at": row["expires_at"], "created_at": row["created_at"], "decided_at": row["decided_at"],
        "reject_reason": row["reject_reason"], "modified_qty": _num(row["modified_qty"]), "executed_qty": _num(qty) if row["status"] in ("approved", "modified") and row["type"] == "trade" else None,
        "order_id": order_id, "disclaimer": DISCLAIMER_TEXT,
    }  # fmt: skip
    view["why"] = _why(
        {
            "kind": row["type"],
            "rule": pl.get("rule"),
            "title": pl["title"],
            "rules_checked": view["rules_checked"],
            "sources": view["sources"],
        }
    )
    return view


async def _order_ids(conn: Connection, ids: list[UUID]) -> dict[UUID, UUID]:
    if not ids:
        return {}
    rows = await conn.fetch(
        "select proposal_id, id from orders where proposal_id = any($1::uuid[]) order by created_at", ids
    )
    return {r["proposal_id"]: r["id"] for r in rows}


# --------------------------------------------------------------------------------------------------
# refresh (generate / expire)
# --------------------------------------------------------------------------------------------------


async def expire_overdue(conn: Connection, user_id: UUID, now: datetime) -> int:
    status = await conn.execute(
        "update proposals set status = 'expired', decided_at = $2 where user_id = $1 and status = 'pending' and expires_at <= $2",
        user_id, now,
    )  # fmt: skip
    return int(status.split()[-1])


async def refresh(conn: Connection, user_id: UUID, ctx: EngineContext) -> dict[str, Any]:
    """Idempotent: expire overdue / resolved proposals, create the ones that should exist and do not yet.
    At most one pending proposal per (symbol, rule); a decided one is not re-created for 24 h."""
    now = ctx.clock.now()
    expired = await expire_overdue(conn, user_id, now)
    state = await load_state(conn, user_id, ctx)
    wanted = {(c.symbol or "", c.rule): c for c in generate_candidates(state)}
    pending = await conn.fetch(
        "select id, symbol, payload from proposals where user_id = $1 and status = 'pending' for update",
        user_id,
    )
    have: set[tuple[str, str]] = set()
    resolved = 0
    for p in pending:
        key = (p["symbol"] or "", p["payload"].get("rule", ""))
        if key in wanted:
            have.add(key)
            continue
        await conn.execute(
            "update proposals set status = 'expired', decided_at = $2, payload = payload || '{\"resolved\": true}'::jsonb where id = $1",
            p["id"],
            now,
        )
        resolved += 1
    created: list[dict[str, Any]] = []
    for key, cand in wanted.items():
        if key in have:
            continue
        recent = await conn.fetchval(
            "select 1 from proposals where user_id = $1 and coalesce(symbol, '') = $2 and payload ->> 'rule' = $3 "
            "and status = any($4::text[]) and decided_at > $5 limit 1",
            user_id, key[0], key[1], list(DECIDED), now - COOLDOWN,
        )  # fmt: skip
        if recent:
            continue
        row = await conn.fetchrow(
            """
            insert into proposals (user_id, portfolio_id, type, symbol, payload, rationale, rules_checked, status, created_at, expires_at)
            values ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9)
            on conflict do nothing returning *
            """,
            user_id, state.portfolio_id, cand.kind, cand.symbol, cand.payload(), cand.rationale, cand.rules_checked, now, now + EXPIRY,
        )  # fmt: skip
        if row is None:
            continue
        await write_audit(
            conn, user_id=user_id, action="proposal.create", entity="proposal", entity_id=str(row["id"]),
            details={"rule": cand.rule, "symbol": cand.symbol, "suggested_qty": cand.suggested_qty, "generator": "risk_engine/1",
                     "inputs": {"equity": float(state.equity), "trades_7d": state.trades_7d, "rules": state.rules.to_dict()}},
        )  # fmt: skip
        view = proposal_view(row)
        created.append(view)
        await hooks.on_created(conn, user_id, _jsonable(view))
    open_n = await conn.fetchval(
        "select count(*) from proposals where user_id = $1 and status = 'pending'", user_id
    )
    return {"created": len(created), "expired": expired + resolved, "open": int(open_n), "items": created}


def _jsonable(view: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (str(v) if isinstance(v, UUID) else v.isoformat() if isinstance(v, datetime) else v)
        for k, v in view.items()
    }


async def refresh_after_trade(conn: Connection, user_id: UUID, ctx: EngineContext) -> None:
    """`paper` after-trade hook: keep proposals in step with the portfolio after every fill / cancel / expiry."""
    await refresh(conn, user_id, ctx)


# --------------------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------------------


async def list_proposals(
    conn: Connection, user_id: UUID, now: datetime, status: str | None, limit: int
) -> list[dict[str, Any]]:
    await expire_overdue(conn, user_id, now)
    rows = await conn.fetch(
        "select * from proposals where user_id = $1 and ($2::text is null or status = $2) order by created_at desc, id limit $3",
        user_id, status, limit,
    )  # fmt: skip
    orders = await _order_ids(conn, [r["id"] for r in rows])
    return [proposal_view(r, orders.get(r["id"])) for r in rows]


async def get_proposal(conn: Connection, user_id: UUID, proposal_id: UUID, now: datetime) -> dict[str, Any]:
    await expire_overdue(conn, user_id, now)
    row = await conn.fetchrow("select * from proposals where id = $1 and user_id = $2", proposal_id, user_id)
    if row is None:
        raise not_found("Proposal not found")
    return proposal_view(row, (await _order_ids(conn, [proposal_id])).get(proposal_id))


# --------------------------------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------------------------------


async def decide(
    conn: Connection, user_id: UUID, proposal_id: UUID, decision: str, ctx: EngineContext,
    *, reason: str | None = None, qty: int | None = None,
) -> dict[str, Any]:  # fmt: skip
    """approve | reject | modify. Trade approve/modify executes exactly one paper order (idempotent per proposal);
    any failure (stale quote, no shares, ...) rolls the whole decision back and the proposal stays pending."""
    now = ctx.clock.now()
    await get_portfolio_row(
        conn, user_id, lock=True
    )  # same lock order as place_order: portfolio, then proposal
    await expire_overdue(conn, user_id, now)
    row = await conn.fetchrow(
        "select * from proposals where id = $1 and user_id = $2 for update", proposal_id, user_id
    )
    if row is None:
        raise not_found("Proposal not found")
    if row["status"] != "pending":
        msg = (
            "Proposal expired and can no longer be decided"
            if row["status"] == "expired"
            else f"Proposal is already {row['status']}"
        )
        raise conflict(msg, {"status": row["status"]})
    payload = row["payload"]
    is_trade = row["type"] == "trade"
    if decision == "reject" and reason is not None and reason not in templates.REJECT_REASONS:
        raise validation_error("Unknown reject reason", {"allowed": list(templates.REJECT_REASONS)})
    if decision != "reject" and reason is not None:
        raise validation_error("reason is only valid when rejecting")
    if decision == "modify" and not is_trade:
        raise validation_error("Review proposals cannot be modified")
    if decision != "modify" and qty is not None:
        raise validation_error("qty is only valid when modifying")

    order: dict[str, Any] | None = None
    new_status = {"approve": "approved", "reject": "rejected", "modify": "modified"}[decision]
    reward = {"approve": hooks.REWARD_APPROVE, "reject": hooks.REWARD_REJECT, "modify": hooks.REWARD_MODIFY}[
        decision
    ]
    exec_qty: int | None = None
    if is_trade and decision in {"approve", "modify"}:
        held_row = await conn.fetchrow(
            "select qty from positions where portfolio_id = $1 and symbol = $2",
            row["portfolio_id"],
            row["symbol"],
        )
        held = int(held_row["qty"]) if held_row else 0
        if held <= 0:
            await conn.execute(
                "update proposals set status = 'expired', decided_at = $2 where id = $1", proposal_id, now
            )
            raise conflict("This proposal no longer applies: the position is closed", {"status": "expired"})
        if decision == "modify":
            if qty is None or qty < 1 or qty > held:
                raise validation_error("qty must be between 1 and the shares held", {"held": held})
            exec_qty = qty
        else:
            exec_qty = min(int(payload["suggested_qty"]), held)
    await conn.execute(
        "update proposals set status = $2, reject_reason = $3, modified_qty = $4, decided_at = $5 where id = $1",
        proposal_id, new_status, reason if decision == "reject" else None, Decimal(exec_qty) if decision == "modify" and exec_qty else None, now,
    )  # fmt: skip
    if exec_qty:
        key = str(uuid5(_KEY_NS, f"proposal:{proposal_id}:execute"))
        result = await paper.place_order(
            conn, user_id,
            paper.OrderIntent(row["symbol"], payload.get("side") or "sell", "market", Decimal(exec_qty), None, proposal_id, False, "proposal"),
            key, ctx,
        )  # fmt: skip
        order = dump(OrderOut, result.order)
    ent = {"proposal_id": str(proposal_id), "ticker": row["symbol"], "rule": payload.get("rule")}
    props: dict[str, Any] = {"decision": decision}
    if reason:
        props["reason"] = reason
    if decision == "modify" and exec_qty:
        props["delta"] = round(exec_qty / max(1, int(payload["suggested_qty"])), 4)
        props["qty"] = exec_qty
    await emit_event(conn, user_id=user_id, ts=now, type=f"proposal_{new_status}", entity=ent, props=props)
    await write_audit(
        conn, user_id=user_id, action=f"proposal.{decision}", entity="proposal", entity_id=str(proposal_id),
        details={"rule": payload.get("rule"), "symbol": row["symbol"], "reason": reason, "qty": exec_qty, "order_id": str(order["id"]) if order else None},
    )  # fmt: skip
    fresh = await conn.fetchrow("select * from proposals where id = $1", proposal_id)
    view = proposal_view(fresh, order["id"] if order else None)
    await hooks.on_decision(conn, user_id, _jsonable(view), reward)
    return {"proposal": view, "order": order}
