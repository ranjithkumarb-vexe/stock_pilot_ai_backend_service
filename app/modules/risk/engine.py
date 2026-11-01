"""Deterministic risk engine (SPEC 7.6): pure functions, no I/O, no clock, no randomness.

* `compute_metrics` - weights, sector weights, HHI / effective N, drawdown, 60 d volatility, beta, trades/7 d.
  Insufficient or degenerate history yields `None` plus a flag (never an exception - T-RISK-04).
* `validate` - pre-trade check of a proposed trade against the user's rules. `allowed` is False when any rule
  would be broken; the engine has VETO power (ranking only sees `allowed=True` items). Overriding a soft
  violation is the paper engine's explicit, logged user action - the engine never loosens a rule.
* `default_rules_for_tier` - SPEC 7.6 defaults.
* `suggest_reduction` - the share count that brings a position/sector back to its cap (design `pq`).

The check labels/details are word-for-word the design's `check()` so the UI shows the same checklist.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Literal

import numpy as np

from app.modules.risk.models import (
    CurveSummary,
    Portfolio,
    PortfolioMetrics,
    RuleCheck,
    RuleKey,
    Rules,
    RuleStatus,
    TradeProposal,
    ValidationResult,
    Violation,
)

ZERO = Decimal(0)
HUNDRED = Decimal(100)
TRADING_DAYS = 252
VOL_WINDOW = 60
BETA_MAX_WINDOW = 252
MIN_RETURNS = 20  # below this, vol/beta are None + `insufficient_history`
WARN_FRACTION = Decimal("0.9")  # 90 % of a cap -> "warn"
HHI_FLAG = 0.25

_TIER_DEFAULTS: dict[str, Rules] = {
    "conservative": Rules(Decimal(10), Decimal(25), Decimal(10), 3, Decimal(10)),
    "balanced": Rules(Decimal(15), Decimal(35), Decimal(15), 5, Decimal(5)),
    "aggressive": Rules(Decimal(25), Decimal(50), Decimal(25), 10, Decimal(0)),
}


def default_rules_for_tier(tier: int) -> Rules:
    """T1-2 conservative, T3 balanced, T4-5 aggressive (SPEC 7.6 table)."""
    if isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= 5:
        raise ValueError(f"risk tier must be an integer 1-5, got {tier!r}")
    return _TIER_DEFAULTS["conservative" if tier <= 2 else "balanced" if tier == 3 else "aggressive"]


# --------------------------------------------------------------------------------------------------
# formatting helpers (match the UI's wording)
# --------------------------------------------------------------------------------------------------


def _pct_text(d: Decimal) -> str:
    """`10` -> '10', `12.50` -> '12.5' (rule limits in labels)."""
    s = format(d.normalize(), "f")
    return s


def _fixed(value: Decimal, places: int) -> str:
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")


def _money0(value: Decimal) -> str:
    """JS `Math.round(v).toLocaleString()` for US locale."""
    return f"{int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)):,}"


def _shares(q: Decimal) -> str:
    return format(q.normalize(), "f") if q != q.to_integral_value() else str(int(q))


# --------------------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------------------


def _returns(levels: Sequence[float]) -> np.ndarray | None:
    arr = np.asarray([float(x) for x in levels], dtype=float)
    if arr.size < 2 or not np.all(np.isfinite(arr)) or np.any(arr[:-1] <= 0):
        return None
    with np.errstate(all="ignore"):
        out = arr[1:] / arr[:-1] - 1.0
    return out if np.all(np.isfinite(out)) else None


def _drawdowns(levels: Sequence[float]) -> tuple[float, float] | None:
    arr = np.asarray([float(x) for x in levels], dtype=float)
    if arr.size < 1 or not np.all(np.isfinite(arr)) or np.any(arr <= 0):
        return None  # an equity curve that reaches zero / goes negative is data corruption, not a drawdown
    peak = np.maximum.accumulate(arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, 1.0 - arr / peak, 0.0)
    return float(dd[-1]) * 100.0, float(dd.max()) * 100.0


def compute_metrics(
    portfolio: Portfolio,
    *,
    equity_curve: Sequence[float | Decimal] | None = None,
    benchmark_curve: Sequence[float | Decimal] | None = None,
) -> PortfolioMetrics:
    """All risk metrics for `portfolio`.

    `equity_curve` / `benchmark_curve` are level series (equity $ and e.g. SPY close), oldest first, aligned
    on the same dates. When their lengths differ the most recent `min(len)` points are paired. Without a
    curve the history-based metrics are None and flagged.
    """
    flags: list[str] = []
    equity = portfolio.equity
    invested = portfolio.invested
    holdings = [p for p in portfolio.positions if p.qty > 0]

    weights: dict[str, Decimal] = {}
    inv_w: dict[str, Decimal] = {}
    sector_w: dict[str, Decimal] = {}
    cash_w = ZERO
    if equity > 0:
        cash_w = portfolio.cash / equity
        for p in holdings:
            weights[p.symbol] = weights.get(p.symbol, ZERO) + p.value / equity
            sec = p.sector or "Unclassified"
            sector_w[sec] = sector_w.get(sec, ZERO) + p.value / equity
    else:
        flags.append("empty_portfolio")
    if invested > 0:
        for p in holdings:
            inv_w[p.symbol] = inv_w.get(p.symbol, ZERO) + p.value / invested
    hhi: float | None = None
    eff_n: float | None = None
    if inv_w:
        hhi = float(sum((w * w for w in inv_w.values()), ZERO))
        eff_n = 1.0 / hhi if hhi > 0 else None
    max_pos = max(weights.items(), key=lambda kv: (kv[1], kv[0])) if weights else None
    max_sec = max(sector_w.items(), key=lambda kv: (kv[1], kv[0])) if sector_w else None

    day_change = sum((p.qty * (p.price - p.prev_close) for p in holdings if p.prev_close is not None), ZERO)

    dd_now: float | None = None
    dd_max: float | None = None
    vol: float | None = None
    beta: float | None = None
    if equity_curve is not None and len(equity_curve) >= 1:
        dds = _drawdowns([float(x) for x in equity_curve])
        if dds is None:
            flags.append("invalid_equity_curve")
        else:
            dd_now, dd_max = dds
        port_ret = _returns([float(x) for x in equity_curve])
        if port_ret is None:
            if len(equity_curve) >= 2:
                flags.append("invalid_equity_curve")
            flags.append("insufficient_history")
        elif port_ret.size < MIN_RETURNS:
            flags.append("insufficient_history")
        else:
            window = port_ret[-VOL_WINDOW:]
            sd = float(window.std(ddof=1))
            if sd == 0.0 or not math.isfinite(sd):
                flags.append("zero_variance")
            else:
                vol = sd * math.sqrt(TRADING_DAYS) * 100.0
            if benchmark_curve is None:
                flags.append("no_benchmark")
            else:
                bench_ret = _returns([float(x) for x in benchmark_curve])
                if bench_ret is None:
                    flags.append("invalid_benchmark")
                else:
                    n = min(port_ret.size, bench_ret.size, BETA_MAX_WINDOW)
                    if n < MIN_RETURNS:
                        flags.append("insufficient_history")
                    else:
                        rp, rb = port_ret[-n:], bench_ret[-n:]
                        var_b = float(rb.var(ddof=1))
                        if var_b == 0.0 or float(rp.var(ddof=1)) == 0.0:
                            flags.append("zero_variance")
                        else:
                            beta = float(np.cov(rp, rb, ddof=1)[0, 1]) / var_b
    else:
        flags.append("insufficient_history")

    return PortfolioMetrics(
        equity=equity,
        cash=portfolio.cash,
        invested=invested,
        cash_weight=cash_w,
        weights=weights,
        invested_weights=inv_w,
        sector_weights=sector_w,
        hhi=hhi,
        effective_n=eff_n,
        max_position=max_pos,
        max_sector=max_sec,
        drawdown_pct=dd_now,
        max_drawdown_pct=dd_max,
        vol_60d=vol,
        beta=beta,
        trades_7d=portfolio.trades_7d,
        day_change=day_change,
        flags=tuple(dict.fromkeys(flags)),
    )


# --------------------------------------------------------------------------------------------------
# rule status / current breaches
# --------------------------------------------------------------------------------------------------


def _state(current: Decimal, limit: Decimal, *, kind: Literal["max", "min"], inclusive: bool = False) -> str:
    if kind == "max":
        breach = current >= limit if inclusive else current > limit
        if breach:
            return "breach"
        return "warn" if limit > 0 and current >= limit * WARN_FRACTION else "ok"
    if limit <= 0:
        return "ok"
    if current < limit:
        return "breach"
    return "warn" if current < limit * Decimal("1.25") else "ok"


def rule_statuses(metrics: PortfolioMetrics, rules: Rules) -> list[RuleStatus]:
    """Current standing against each rule (GET /portfolio/risk `rules[]`)."""
    pos_sym, pos_w = metrics.max_position or (None, ZERO)
    sec_name, sec_w = metrics.max_sector or (None, ZERO)
    out: list[RuleStatus] = []
    pos_cur = pos_w * HUNDRED
    out.append(RuleStatus("pos", rules.pos, pos_cur, _state(pos_cur, rules.pos, kind="max"), pos_sym))  # type: ignore[arg-type]
    sec_cur = sec_w * HUNDRED
    out.append(RuleStatus("sec", rules.sec, sec_cur, _state(sec_cur, rules.sec, kind="max"), sec_name))  # type: ignore[arg-type]
    dd_cur = Decimal(str(round(metrics.drawdown_pct, 6))) if metrics.drawdown_pct is not None else ZERO
    out.append(RuleStatus("dd", rules.dd, dd_cur, _state(dd_cur, rules.dd, kind="max", inclusive=True), None))  # type: ignore[arg-type]
    t_cur = Decimal(metrics.trades_7d)
    t_lim = Decimal(rules.trades)
    t_state = (
        "breach" if t_cur > t_lim else ("warn" if t_lim > 0 and t_cur >= t_lim * Decimal("0.8") else "ok")
    )
    out.append(RuleStatus("trades", t_lim, t_cur, t_state, None))  # type: ignore[arg-type]
    cash_cur = metrics.cash_weight * HUNDRED
    out.append(RuleStatus("cash", rules.cash, cash_cur, _state(cash_cur, rules.cash, kind="min"), None))  # type: ignore[arg-type]
    return out


def current_violations(metrics: PortfolioMetrics, rules: Rules) -> list[Violation]:
    """Breaches that exist RIGHT NOW (per position and per sector, not just the worst one)."""
    out: list[Violation] = []
    for sym, w in sorted(metrics.weights.items(), key=lambda kv: -kv[1]):
        cur = w * HUNDRED
        if cur > rules.pos:
            out.append(
                Violation(
                    "pos",
                    f"Max position {_pct_text(rules.pos)}%",
                    f"{sym} is {_fixed(cur, 1)}% of your portfolio (limit {_pct_text(rules.pos)}%).",
                    rules.pos,
                    cur,
                )
            )
    for sec, w in sorted(metrics.sector_weights.items(), key=lambda kv: -kv[1]):
        cur = w * HUNDRED
        if cur > rules.sec:
            out.append(
                Violation(
                    "sec",
                    f"Max sector {_pct_text(rules.sec)}%",
                    f"{sec} is {_fixed(cur, 1)}% of your portfolio (limit {_pct_text(rules.sec)}%).",
                    rules.sec,
                    cur,
                )
            )
    if metrics.drawdown_pct is not None and Decimal(str(metrics.drawdown_pct)) >= rules.dd > 0:
        dd = Decimal(str(round(metrics.drawdown_pct, 4)))
        out.append(
            Violation(
                "dd",
                f"Drawdown alert {_pct_text(rules.dd)}%",
                f"Portfolio is {_fixed(dd, 1)}% below its peak (alert at {_pct_text(rules.dd)}%).",
                rules.dd,
                dd,
            )
        )
    if metrics.trades_7d > rules.trades:
        out.append(
            Violation(
                "trades",
                f"Trades per week {rules.trades}",
                f"{metrics.trades_7d} trades in the last 7 days (limit {rules.trades}).",
                Decimal(rules.trades),
                Decimal(metrics.trades_7d),
            )
        )
    if rules.cash > 0 and metrics.equity > 0 and metrics.cash_weight * HUNDRED < rules.cash:
        cur = metrics.cash_weight * HUNDRED
        out.append(
            Violation(
                "cash",
                f"Min cash buffer {_pct_text(rules.cash)}%",
                f"Cash is {_fixed(cur, 1)}% of your portfolio (minimum {_pct_text(rules.cash)}%).",
                rules.cash,
                cur,
            )
        )
    return out


# --------------------------------------------------------------------------------------------------
# pre-trade validation
# --------------------------------------------------------------------------------------------------


def validate(proposal: TradeProposal, portfolio: Portfolio, rules: Rules) -> ValidationResult:
    """Judge one trade against the user's rules.

    Mirrors the design's `check()`: sells only need shares held (and count toward trades/week as a note);
    buys need cash and must keep position <= max position, sector <= max sector, cash >= buffer and
    trades/week below the limit. `violations` hold everything that blocks; `warnings` are advisory.
    """
    equity = portfolio.equity
    held = next((p for p in portfolio.positions if p.symbol == proposal.symbol), None)
    held_qty = held.qty if held else ZERO
    held_val = held.value if held else ZERO
    sector = proposal.sector or (held.sector if held else None)
    value = proposal.qty * proposal.price

    checks: list[RuleCheck] = []
    violations: list[Violation] = []
    warnings: list[Violation] = []

    def add(
        key: RuleKey | str,
        ok: bool,
        label: str,
        detail: str,
        *,
        soft: bool = False,
        violation: Violation | None = None,
    ) -> None:
        severity = "pass" if ok else ("note" if soft else "blocked")
        checks.append(RuleCheck(key, ok, severity, label, detail))  # type: ignore[arg-type]
        if not ok and violation is not None:
            (warnings if soft else violations).append(violation)

    if not proposal.qty >= 1:
        add(
            "quantity",
            False,
            "Quantity",
            "Enter at least 1 share",
            violation=Violation(
                "quantity", "Quantity", "Enter at least 1 share", Decimal(1), proposal.qty, True
            ),
        )
    if proposal.side == "sell":
        ok = proposal.qty <= held_qty
        detail = (
            f"Selling {_shares(proposal.qty)} of {_shares(held_qty)} shares held"
            if ok
            else f"You hold {_shares(held_qty)} shares and cannot sell {_shares(proposal.qty)}"
        )
        add(
            "shares",
            ok,
            "No shorting (MVP)",
            detail,
            violation=Violation("shares", "No shorting (MVP)", detail, held_qty, proposal.qty, True),
        )
    else:
        detail = f"Cost ${_money0(value)} of ${_money0(portfolio.cash)} available"
        add(
            "funds",
            value <= portfolio.cash,
            "Paper cash",
            detail,
            violation=Violation("funds", "Paper cash", detail, portfolio.cash, value, True),
        )
        new_pos = ((held_val + value) / equity * HUNDRED) if equity > 0 else HUNDRED
        label = f"Max position {_pct_text(rules.pos)}%"
        add(
            "pos",
            new_pos <= rules.pos,
            label,
            f"{proposal.symbol} would be {_fixed(new_pos, 1)}%",
            violation=Violation(
                "pos",
                label,
                f"{proposal.symbol} would be {_fixed(new_pos, 1)}% (limit {_pct_text(rules.pos)}%).",
                rules.pos,
                new_pos,
            ),
        )
        sec_now = sum(
            (
                p.value
                for p in portfolio.positions
                if (p.sector or "Unclassified") == (sector or "Unclassified")
            ),
            ZERO,
        )
        new_sec = ((sec_now + value) / equity * HUNDRED) if equity > 0 else HUNDRED
        sec_name = sector or "Unclassified"
        label = f"Max sector {_pct_text(rules.sec)}%"
        add(
            "sec",
            new_sec <= rules.sec,
            label,
            f"{sec_name} would be {_fixed(new_sec, 1)}%",
            violation=Violation(
                "sec",
                label,
                f"{sec_name} would be {_fixed(new_sec, 1)}% (limit {_pct_text(rules.sec)}%).",
                rules.sec,
                new_sec,
            ),
        )
        if rules.cash > 0:
            cash_after = ((portfolio.cash - value) / equity * HUNDRED) if equity > 0 else ZERO
            label = f"Min cash buffer {_pct_text(rules.cash)}%"
            add(
                "cash",
                cash_after >= rules.cash,
                label,
                f"Cash after would be {_fixed(cash_after, 1)}%",
                violation=Violation(
                    "cash",
                    label,
                    f"Cash after would be {_fixed(cash_after, 1)}% (minimum {_pct_text(rules.cash)}%).",
                    rules.cash,
                    cash_after,
                ),
            )
    label = f"Trades per week {rules.trades}"
    add(
        "trades",
        portfolio.trades_7d < rules.trades,
        label,
        f"{portfolio.trades_7d} in the last 7 days",
        soft=proposal.side == "sell",
        violation=Violation(
            "trades",
            label,
            f"{portfolio.trades_7d} trades in the last 7 days (limit {rules.trades}).",
            Decimal(rules.trades),
            Decimal(portfolio.trades_7d),
        ),
    )

    # advisory: buying while the portfolio is already in a drawdown that crossed the alert level is a note, not a veto
    # (drawdown is a history metric; callers who have it add `Violation('dd', ...)` via current_violations)
    allowed = not violations
    return ValidationResult(allowed, tuple(violations), tuple(warnings), tuple(checks))


# --------------------------------------------------------------------------------------------------
# sizing helpers used by proposals
# --------------------------------------------------------------------------------------------------


def suggest_reduction(portfolio: Portfolio, rules: Rules, *, rule: Literal["pos", "sec"], symbol: str) -> int:
    """Whole shares of `symbol` to sell so the position (rule `pos`) or its sector (`sec`) is back at its cap.

    Design `pq`: ceil(excess_value / price), capped at the shares held, 0 when nothing is over the cap.
    """
    equity = portfolio.equity
    cur = next((p for p in portfolio.positions if p.symbol == symbol and p.qty > 0), None)
    if cur is None or equity <= 0 or cur.price <= 0:
        return 0
    if rule == "pos":
        excess = cur.value - rules.pos / HUNDRED * equity
    else:
        sec = cur.sector or "Unclassified"
        sec_value = sum((p.value for p in portfolio.positions if (p.sector or "Unclassified") == sec), ZERO)
        excess = sec_value - rules.sec / HUNDRED * equity
    if excess <= 0:
        return 0
    shares = (excess / cur.price).to_integral_value(rounding=ROUND_CEILING)
    return int(max(ZERO, min(cur.qty.to_integral_value(rounding=ROUND_CEILING), shares)))


def curve_summary(
    equity_curve: Sequence[float | Decimal], benchmark_curve: Sequence[float | Decimal] | None = None
) -> CurveSummary:
    """Period return, volatility, beta and drawdowns over the WHOLE curve (compute_metrics uses 60 d / 252 d
    windows). Same data-quality rules: degenerate input gives None + a flag, never an exception."""
    flags: list[str] = []
    levels = [float(x) for x in equity_curve]
    ret_pct: float | None = None
    if len(levels) >= 2 and levels[0] > 0 and math.isfinite(levels[-1]):
        ret_pct = (levels[-1] / levels[0] - 1.0) * 100.0
    bench_pct: float | None = None
    bench = [float(x) for x in benchmark_curve] if benchmark_curve is not None else None
    if bench is not None and len(bench) >= 2 and bench[0] > 0 and math.isfinite(bench[-1]):
        bench_pct = (bench[-1] / bench[0] - 1.0) * 100.0
    dds = _drawdowns(levels) if levels else None
    rp = _returns(levels)
    vol: float | None = None
    beta: float | None = None
    if rp is None or rp.size < MIN_RETURNS:
        flags.append("insufficient_history")
    else:
        sd = float(rp.std(ddof=1))
        if sd == 0.0 or not math.isfinite(sd):
            flags.append("zero_variance")
        else:
            vol = sd * math.sqrt(TRADING_DAYS) * 100.0
            rb = _returns(bench) if bench is not None else None
            if rb is None:
                flags.append("no_benchmark")
            else:
                n = min(rp.size, rb.size, BETA_MAX_WINDOW)
                if n >= MIN_RETURNS and float(rb[-n:].var(ddof=1)) > 0.0:
                    beta = float(np.cov(rp[-n:], rb[-n:], ddof=1)[0, 1]) / float(rb[-n:].var(ddof=1))
                else:
                    flags.append("insufficient_history" if n < MIN_RETURNS else "zero_variance")
    return CurveSummary(
        ret_pct,
        bench_pct,
        vol,
        beta,
        None if dds is None else dds[0],
        None if dds is None else dds[1],
        tuple(dict.fromkeys(flags)),
    )
