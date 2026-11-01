"""Revealed risk, risk-gap bookkeeping and the five bias detectors (pure)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.modules.personalization import bias as b
from app.modules.personalization.revealed_risk import compute_revealed_risk, gap_state, tier_from

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
D0 = date(2026, 5, 1)


def series(returns: list[float], start: float = 100.0) -> dict[date, float]:
    out = {D0: start}
    px = start
    for i, r in enumerate(returns, 1):
        px *= 1 + r
        out[D0 + timedelta(days=i)] = px
    return out


def noise(seed: int, n: int = 80, sd: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0004, sd) for _ in range(n)]


# ---------------------------------------------------------------------------- revealed risk
def test_tier_mapping_is_monotone_and_bounded() -> None:
    tiers = [tier_from(v, 1.0) or 0 for v in (3, 8, 12, 18, 30, 60)]
    assert tiers == sorted(tiers) and tiers[0] >= 1.0 and tiers[-1] <= 5.0
    assert tier_from(None, 1.0) is None
    assert tier_from(11.0, None) == 2.0
    assert tier_from(60.0, 2.5) == 5.0


def test_beta_and_vol_from_a_levered_benchmark() -> None:
    spy = noise(1)
    stock = [2.0 * r for r in spy]  # exactly twice the market
    out = compute_revealed_risk({"AAA": 1000.0}, {"AAA": series(stock)}, series(spy))
    assert out.beta == pytest.approx(2.0, abs=0.02) and out.n_obs == 60
    assert out.vol_pct is not None and out.vol_pct == pytest.approx(2 * 0.01 * 252**0.5 * 100, rel=0.3)
    assert out.tier is not None and out.tier > 3.5
    calm = compute_revealed_risk({"AAA": 1.0}, {"AAA": series([0.2 * r for r in spy])}, series(spy))
    assert calm.tier is not None and calm.tier < 2.0


def test_weights_follow_market_value() -> None:
    spy = noise(2)
    hi, lo = series([2 * r for r in spy]), series([0.1 * r for r in spy])
    mostly_lo = compute_revealed_risk({"HI": 100.0, "LO": 900.0}, {"HI": hi, "LO": lo}, series(spy))
    mostly_hi = compute_revealed_risk({"HI": 900.0, "LO": 100.0}, {"HI": hi, "LO": lo}, series(spy))
    assert mostly_lo.beta is not None and mostly_hi.beta is not None and mostly_hi.beta > mostly_lo.beta + 1.0


def test_thin_or_degenerate_data_returns_none_and_flags_not_exceptions() -> None:
    """T-RISK-04 spirit: zero variance / new listing -> None + flag, never raises."""
    flat = dict.fromkeys(series([0.0] * 60), 100.0)
    out = compute_revealed_risk({"AAA": 10.0}, {"AAA": flat}, series(noise(3)))
    assert (out.tier, out.vol_pct, out.beta) == (None, None, None) and "zero_variance" in out.flags
    assert (
        "insufficient_history" in compute_revealed_risk({"A": 1.0}, {"A": series(noise(4, 10))}, None).flags
    )
    assert compute_revealed_risk({}, {}, None).flags == ("no_positions",)
    assert "no_price_history" in compute_revealed_risk({"A": 1.0}, {}, None).flags
    no_bench = compute_revealed_risk({"A": 1.0}, {"A": series(noise(5))}, None)
    assert no_bench.beta is None and no_bench.tier is not None and "no_benchmark" in no_bench.flags
    partial = compute_revealed_risk({"A": 1.0, "B": 1.0}, {"A": series(noise(6))}, series(noise(7)))
    assert "missing_prices" in partial.flags and partial.tier is not None
    flat_bench = compute_revealed_risk(
        {"A": 1.0}, {"A": series(noise(8))}, dict.fromkeys(series(noise(8)), 100.0)
    )
    assert flat_bench.beta is None and "zero_benchmark_variance" in flat_bench.flags


# ---------------------------------------------------------------------------- risk gap (T-PERS-04)
TODAY = date(2026, 10, 30)


def test_gap_below_one_resets_and_never_flags() -> None:
    g = gap_state(
        stated=3, revealed=3.9, previous_days=20, previous_since=TODAY - timedelta(days=20), today=TODAY
    )
    assert g["gap_days"] == 0 and g["since"] is None and g["flag"] is False


def test_gap_starts_counting_and_flags_after_14_days() -> None:
    first = gap_state(stated=1, revealed=4.5, previous_days=0, previous_since=None, today=TODAY)
    assert (
        first["gap"] == 3.5 and first["gap_days"] == 0 and first["flag"] is False and first["since"] == TODAY
    )
    later = gap_state(
        stated=1,
        revealed=4.5,
        previous_days=0,
        previous_since=TODAY,
        today=TODAY + timedelta(days=14),
    )
    assert later["gap_days"] == 14 and later["flag"] is True
    day13 = gap_state(
        stated=1, revealed=4.5, previous_days=0, previous_since=TODAY, today=TODAY + timedelta(days=13)
    )
    assert day13["flag"] is False


def test_gap_continues_from_a_seeded_day_count_without_a_since_date() -> None:
    g = gap_state(stated=4, revealed=5.0, previous_days=17, previous_since=None, today=TODAY)
    assert g["gap"] == 1.0 and g["gap_days"] == 17 and g["flag"] is True  # Alex's seeded 17 days survive
    assert (
        gap_state(stated=4, revealed=3.0, previous_days=5, previous_since=None, today=TODAY)["gap"] == -1.0
    )  # negative gap counts too


def test_gap_unknown_inputs_keep_previous_state() -> None:
    g = gap_state(stated=None, revealed=4.0, previous_days=9, previous_since=None, today=TODAY)
    assert g["gap"] is None and g["gap_days"] == 9 and g["flag"] is False
    assert (
        gap_state(stated=2, revealed=None, previous_days=20, previous_since=None, today=TODAY)["flag"] is True
    )


# ---------------------------------------------------------------------------- bias detectors
def T(day: int, symbol: str, side: str, px: float, qty: float = 10) -> b.Trade:
    return b.Trade(datetime(2026, 6, 1, 15, tzinfo=UTC) + timedelta(days=day), symbol, side, qty, px)  # type: ignore[arg-type]


def test_overtrading_states_follow_the_rule_and_turnover() -> None:
    week = [
        b.Trade(NOW - timedelta(days=i), "AAA", "buy", 1, 10.0) for i in range(1, 10)
    ]  # 7 land inside 7 days
    r = b.detect_overtrading(week, now=NOW, rule_limit=10, portfolio_value=10_000)
    assert r.n == 7 and r.state == "ok"
    nine = [b.Trade(NOW - timedelta(hours=10 * i), "AAA", "buy", 1, 10.0) for i in range(9)]
    watch = b.detect_overtrading(nine, now=NOW, rule_limit=10, portfolio_value=10_000)
    assert watch.state == "watch" and watch.value == "9 (rule 10)"  # the design's Alex numbers
    flag = b.detect_overtrading(nine, now=NOW, rule_limit=5, portfolio_value=10_000)
    assert flag.state == "flag"
    quiet = b.detect_overtrading(
        [b.Trade(NOW - timedelta(days=1), "A", "buy", 1, 1.0)], now=NOW, rule_limit=3, portfolio_value=1000
    )
    assert quiet.state == "ok" and quiet.value == "1 (rule 3)"
    churn = [b.Trade(NOW - timedelta(days=30), "A", "sell", 100, 100.0)]
    assert (
        b.detect_overtrading(churn, now=NOW, rule_limit=10, portfolio_value=10_000).state == "watch"
    )  # turnover > 100%
    assert b.detect_overtrading([], now=NOW, rule_limit=None, portfolio_value=0).value == "0 (rule 5)"


def disposition_trades(biased: bool) -> tuple[list[b.Trade], Any]:
    """12 sells. Biased: winners sold quickly, losers held. Neutral: sells independent of P&L."""
    trades: list[b.Trade] = []
    prices: dict[tuple[str, date], float] = {}
    for i in range(6):
        w, lo = f"W{i}", f"L{i}"
        trades += [T(i * 3, w, "buy", 100), T(i * 3, lo, "buy", 100)]
        prices[(w, T(i * 3 + 1, w, "buy", 1).ts.date())] = 120.0
        prices[(lo, T(i * 3 + 1, lo, "buy", 1).ts.date())] = 80.0
        trades.append(T(i * 3 + 1, w, "sell", 120))  # realised gain
        if not biased:
            trades.append(T(i * 3 + 1, lo, "sell", 80))  # realised loss, same day
    if biased:
        for i in range(6):  # keep sells >= 10 with a few more gains
            trades += [T(40 + i, f"X{i}", "buy", 50), T(41 + i, f"X{i}", "sell", 60)]
            prices[(f"X{i}", T(41 + i, "X", "buy", 1).ts.date())] = 60.0

    def close_on(sym: str, day: date) -> float | None:
        if (sym, day) in prices:
            return prices[(sym, day)]
        return 80.0 if sym.startswith("L") else 120.0 if sym.startswith("W") else None

    return trades, close_on


def test_disposition_flags_biased_sellers_and_not_neutral_ones() -> None:
    t, close = disposition_trades(biased=True)
    r = b.detect_disposition(t, close)
    assert r.state == "flag" and r.raw is not None and r.raw > 0.10 and r.n >= 10
    t2, close2 = disposition_trades(biased=False)
    n = b.detect_disposition(t2, close2)
    assert n.state == "ok" and n.raw is not None and n.raw <= 0.05


def test_disposition_needs_ten_sells() -> None:
    t = [T(0, "A", "buy", 10), T(1, "A", "sell", 12)]
    r = b.detect_disposition(t, lambda s, d: None)
    assert r.state == "ok" and r.raw is None and "needs 10 sells" in r.value and r.n == 1
    assert b.detect_disposition([], lambda s, d: None).n == 0
    # selling something never bought (no shorting) is ignored
    assert b.detect_disposition([T(0, "A", "sell", 12)], lambda s, d: None).n == 0


def test_concentration_hhi() -> None:
    assert b.detect_concentration({str(i): 1 for i in range(8)}).state == "ok"  # 0.125
    assert b.detect_concentration({str(i): 1 for i in range(5)}).state == "watch"  # 0.20
    assert b.detect_concentration({"A": 70, "B": 30}).state == "flag"
    r = b.detect_concentration({"A": 60, "B": 20, "C": 20})
    assert r.state == "flag" and r.value.startswith("0.44")
    assert b.detect_concentration({"A": 1, "B": 1, "C": 1, "D": 1}).raw == 0.25
    assert b.detect_concentration({}).state == "ok" and b.detect_concentration({"A": 0}).n == 0


def test_spike_dates_find_big_moves_and_volume_surges() -> None:
    rng = random.Random(9)
    bars: list[tuple[date, float, float | None]] = []
    px = 100.0
    for i in range(40):
        px *= 1 + rng.gauss(0, 0.005)
        bars.append((D0 + timedelta(days=i), px, 1000.0 + rng.gauss(0, 20)))
    px *= 1.08  # +8% day
    bars.append((D0 + timedelta(days=40), px, 1000.0))
    bars.append((D0 + timedelta(days=41), px, 9000.0))  # volume surge, price flat
    sp = b.spike_dates(bars)
    assert D0 + timedelta(days=40) in sp and D0 + timedelta(days=41) in sp
    assert (
        len(sp - {D0 + timedelta(days=40), D0 + timedelta(days=41)}) <= 8
    )  # <= 20% false positives on noise
    assert b.spike_dates(bars[:5]) == set()


def test_herding_counts_buys_within_two_days_of_a_spike() -> None:
    spike = date(2026, 6, 10)
    spikes = {"A": {spike}}
    buys = [b.Trade(datetime(2026, 6, 10 + k, 15, tzinfo=UTC), "A", "buy", 1, 1.0) for k in (0, 1, 2)]
    buys += [
        b.Trade(datetime(2026, 6, 20, 15, tzinfo=UTC), "A", "buy", 1, 1.0),
        b.Trade(datetime(2026, 6, 9, 15, tzinfo=UTC), "A", "buy", 1, 1.0),
    ]
    r = b.detect_herding(buys, spikes)
    assert r.raw == 0.6 and r.state == "flag" and r.value == "60% (flag at 40%)"
    assert b.detect_herding(buys, {}).state == "ok"
    assert b.detect_herding(buys[:3], spikes).value.startswith("n/a")


def test_loss_aversion_ratio_and_minimums() -> None:
    d = b.ReduceDecision
    biased = [d(True, True)] * 5 + [d(False, False)] * 5
    r = b.detect_loss_aversion(biased)
    assert r.state == "flag" and r.raw is not None and r.raw > 2.0
    even = [d(True, True), d(False, True), d(True, False), d(False, False)] * 3
    assert b.detect_loss_aversion(even).state == "ok"
    assert b.detect_loss_aversion([d(True, True)] * 4).value.startswith("n/a")
    assert b.BiasResult("x", "X", "m", "v", "ok", None, 0).as_dict()["state"] == "ok"
