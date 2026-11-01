"""Behavioural-bias detectors (SPEC 10.3 G). Pure functions over plain records; the loaders live elsewhere.

Output shape follows CONTRACT `/profile.bias`: `{name, metric, value, state: ok|watch|flag}` where `value` is the
display string the UI prints (e.g. `0.24 (flag at 0.10)`), plus `raw` (number|null) and `n` (sample size).
A detector with too little data returns state `ok` and an explicit "needs N ..." value - it never guesses.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

State = Literal["ok", "watch", "flag"]

DISPOSITION_FLAG = 0.10
DISPOSITION_WATCH = 0.05
DISPOSITION_MIN_SELLS = 10
HERDING_FLAG = 0.40
HERDING_WATCH = 0.25
HERDING_MIN_BUYS = 5
LOSS_AVERSION_FLAG = 2.0
LOSS_AVERSION_WATCH = 1.5
LOSS_AVERSION_MIN_PER_GROUP = 3
HHI_FLAG = 0.25
HHI_WATCH = 0.18
SPIKE_Z = 2.0
SPIKE_WINDOW_DAYS = 2
DEFAULT_TRADES_RULE = 5

SLUGS = ("overtrading", "disposition", "concentration", "herding", "loss_aversion")


@dataclass(frozen=True, slots=True)
class Trade:
    ts: datetime
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    price: float


@dataclass(frozen=True, slots=True)
class BiasResult:
    slug: str
    name: str
    metric: str
    value: str
    state: State
    raw: float | None
    n: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metric": self.metric,
            "value": self.value,
            "state": self.state,
            "raw": self.raw,
            "n": self.n,
        }


# ------------------------------------------------------------------------------- overtrading
def detect_overtrading(
    trades: Sequence[Trade], *, now: datetime, rule_limit: int | None, portfolio_value: float
) -> BiasResult:
    limit = rule_limit if rule_limit and rule_limit > 0 else DEFAULT_TRADES_RULE
    week = [t for t in trades if now - timedelta(days=7) <= t.ts <= now]
    n7 = len(week)
    window = timedelta(days=90)
    sold = sum(t.qty * t.price for t in trades if t.side == "sell" and now - window <= t.ts <= now)
    turnover = sold * (365.0 / 90.0) / portfolio_value * 100.0 if portfolio_value > 0 else 0.0
    if n7 > limit:
        state: State = "flag"
    elif n7 >= 0.8 * limit or turnover > 100.0:
        state = "watch"
    else:
        state = "ok"
    return BiasResult(
        "overtrading", "Overtrading", "Trades per week", f"{n7} (rule {limit})", state, float(n7), n7
    )


# ------------------------------------------------------------------------------- disposition
def detect_disposition(trades: Sequence[Trade], close_on: Callable[[str, date], float | None]) -> BiasResult:
    """Odean-style PGR - PLR. On every day with a sale, each *other* open position counts as a paper gain or loss
    versus its average cost; the sold lot counts as a realised gain or loss."""
    qty: dict[str, float] = defaultdict(float)
    cost: dict[str, float] = defaultdict(float)  # total cost basis
    rg = rl = pg = pl = 0
    sells = 0
    by_day: dict[date, list[Trade]] = defaultdict(list)
    for t in sorted(trades, key=lambda t: t.ts):
        by_day[t.ts.date()].append(t)
    for day in sorted(by_day):
        day_realised_symbols: set[str] = set()
        had_sale = False
        for t in by_day[day]:
            if t.side == "buy":
                qty[t.symbol] += t.qty
                cost[t.symbol] += t.qty * t.price
                continue
            held = qty[t.symbol]
            if held <= 0:
                continue
            avg = cost[t.symbol] / held
            sells += 1
            had_sale = True
            day_realised_symbols.add(t.symbol)
            if t.price > avg:
                rg += 1
            elif t.price < avg:
                rl += 1
            sold = min(t.qty, held)
            qty[t.symbol] -= sold
            cost[t.symbol] -= sold * avg
        if not had_sale:
            continue
        for sym, q in qty.items():
            if q <= 1e-9 or sym in day_realised_symbols:
                continue
            px = close_on(sym, day)
            if px is None:
                continue
            avg = cost[sym] / q
            if px > avg:
                pg += 1
            elif px < avg:
                pl += 1
    if sells < DISPOSITION_MIN_SELLS:
        return BiasResult(
            "disposition",
            "Disposition effect",
            "PGR - PLR",
            f"n/a (needs {DISPOSITION_MIN_SELLS} sells, has {sells})",
            "ok",
            None,
            sells,
        )
    pgr = rg / (rg + pg) if rg + pg else 0.0
    plr = rl / (rl + pl) if rl + pl else 0.0
    diff = round(pgr - plr, 2)
    state: State = "flag" if diff > DISPOSITION_FLAG else "watch" if diff > DISPOSITION_WATCH else "ok"
    return BiasResult(
        "disposition",
        "Disposition effect",
        "PGR - PLR",
        f"{diff:.2f} (flag at {DISPOSITION_FLAG:.2f})",
        state,
        diff,
        sells,
    )


# ------------------------------------------------------------------------------- concentration
def detect_concentration(values: Mapping[str, float]) -> BiasResult:
    held = {s: v for s, v in values.items() if v > 0}
    total = sum(held.values())
    if total <= 0:
        return BiasResult("concentration", "Concentration", "HHI", "n/a (no positions)", "ok", None, 0)
    hhi = round(sum((v / total) ** 2 for v in held.values()), 2)
    state: State = "flag" if hhi > HHI_FLAG else "watch" if hhi > HHI_WATCH else "ok"
    return BiasResult(
        "concentration", "Concentration", "HHI", f"{hhi:.2f} (flag at {HHI_FLAG:.2f})", state, hhi, len(held)
    )


# ------------------------------------------------------------------------------- herding
def spike_dates(bars: Sequence[tuple[date, float, float | None]], window: int = 60) -> set[date]:
    """Days whose |return| or volume is >= 2 sigma against the trailing `window` days. `bars`: (date, close, volume)."""
    spikes: set[date] = set()
    rows = sorted(bars)
    rets: list[float] = []
    vols: list[float] = []
    for i in range(1, len(rows)):
        d, close, vol = rows[i]
        prev_close = rows[i - 1][1]
        r = close / prev_close - 1.0 if prev_close > 0 else 0.0
        if len(rets) >= 10:
            sd = statistics.pstdev(rets[-window:])
            if sd > 0 and abs(r) / sd >= SPIKE_Z:
                spikes.add(d)
            elif vol is not None and len(vols) >= 10:
                v_sd = statistics.pstdev(vols[-window:])
                if v_sd > 0 and (vol - statistics.fmean(vols[-window:])) / v_sd >= SPIKE_Z:
                    spikes.add(d)
        rets.append(r)
        if vol is not None:
            vols.append(vol)
    return spikes


def detect_herding(trades: Sequence[Trade], spikes: Mapping[str, set[date]]) -> BiasResult:
    buys = [t for t in trades if t.side == "buy"]
    if len(buys) < HERDING_MIN_BUYS:
        return BiasResult(
            "herding",
            "Herding",
            "Buys within 2 days of a spike",
            f"n/a (needs {HERDING_MIN_BUYS} buys, has {len(buys)})",
            "ok",
            None,
            len(buys),
        )
    hits = 0
    for t in buys:
        day = t.ts.date()
        if any((day - timedelta(days=k)) in spikes.get(t.symbol, ()) for k in range(SPIKE_WINDOW_DAYS + 1)):
            hits += 1
    share = hits / len(buys)
    state: State = "flag" if share > HERDING_FLAG else "watch" if share > HERDING_WATCH else "ok"
    return BiasResult(
        "herding",
        "Herding",
        "Buys within 2 days of a spike",
        f"{share * 100:.0f}% (flag at {HERDING_FLAG * 100:.0f}%)",
        state,
        round(share, 3),
        len(buys),
    )


# ------------------------------------------------------------------------------- loss aversion
@dataclass(frozen=True, slots=True)
class ReduceDecision:
    rejected: bool
    loser: bool  # the position was under water when the proposal was made


def detect_loss_aversion(decisions: Sequence[ReduceDecision]) -> BiasResult:
    """Reject rate of 'reduce exposure' proposals for losers / for winners (Laplace-smoothed)."""
    los = [d for d in decisions if d.loser]
    win = [d for d in decisions if not d.loser]
    n = len(decisions)
    if min(len(los), len(win)) < LOSS_AVERSION_MIN_PER_GROUP:
        return BiasResult(
            "loss_aversion",
            "Loss aversion",
            "Reject ratio, losers vs winners",
            f"n/a (needs {LOSS_AVERSION_MIN_PER_GROUP} decisions on winners and losers)",
            "ok",
            None,
            n,
        )

    def rate(group: Sequence[ReduceDecision]) -> float:
        return (sum(d.rejected for d in group) + 0.5) / (len(group) + 1.0)

    ratio = round(rate(los) / rate(win), 2)
    state: State = "flag" if ratio > LOSS_AVERSION_FLAG else "watch" if ratio > LOSS_AVERSION_WATCH else "ok"
    return BiasResult(
        "loss_aversion",
        "Loss aversion",
        "Reject ratio, losers vs winners",
        f"{ratio:.1f} (flag at {LOSS_AVERSION_FLAG:.1f})",
        state,
        ratio,
        n,
    )
