"""Pure paper-engine and ledger tests (no database): decisions, slippage, accounting properties."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.pricesource import Quote
from app.modules.paper import engine
from app.modules.paper.ledger import (
    FillRecord,
    LedgerError,
    Lot,
    Side,
    SplitRecord,
    apply_buy,
    apply_sell,
    apply_split,
    cash_effect,
    q2,
    realised_pnl_of,
    rebuild_positions,
    solve_opening_lot,
    split_effective_at,
)
from app.modules.risk import Portfolio, Position, Rules, TradeProposal, validate

D = Decimal
T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
PROP = settings(
    max_examples=int(os.environ.get("HYPOTHESIS_EXAMPLES", "1000")),
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def quote(price: str = "100", freshness: str = "live") -> Quote:
    return Quote("X", D(price), T0, "test", freshness=freshness)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------- slippage / crossing


@pytest.mark.parametrize(
    ("last", "side", "expected"),
    [
        ("182.40", "buy", "182.49"),
        ("182.40", "sell", "182.31"),
        ("100", "buy", "100.05"),
        ("100", "sell", "99.95"),
        ("0.50", "buy", "0.50"),
    ],
)
def test_slippage_5bps_rounded_to_cents(last: str, side: str, expected: str) -> None:
    assert engine.slipped_price(D(last), side, D(5)) == D(expected)  # type: ignore[arg-type]


@given(
    st.decimals(min_value="0.5", max_value=10000, places=2),
    st.sampled_from(["buy", "sell"]),
    st.integers(0, 100),
)
@PROP
def test_slippage_always_moves_against_the_trader(last: Decimal, side: str, bps: int) -> None:
    px = engine.slipped_price(last, side, D(bps))  # type: ignore[arg-type]
    assert px >= last if side == "buy" else px <= last
    assert abs(px - last) <= last * D(bps) / D(10000) + D("0.005")


@pytest.mark.parametrize(
    ("side", "last", "limit", "crosses"),
    [("buy", "99", "100", True), ("buy", "100", "100", True), ("buy", "101", "100", False),
     ("sell", "101", "100", True), ("sell", "100", "100", True), ("sell", "99", "100", False)],
)  # fmt: skip
def test_limit_crossing(side: str, last: str, limit: str, crosses: bool) -> None:
    assert engine.limit_crosses(side, D(last), D(limit)) is crosses  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------- decisions


def spec(type_: str = "market", side: str = "buy", limit: str | None = None) -> engine.OrderSpec:
    return engine.OrderSpec("X", side, type_, D(10), None if limit is None else D(limit))  # type: ignore[arg-type]


def test_placement_decisions() -> None:
    q = quote("100")
    assert engine.decide_placement(spec(), q, True, D(5)) == engine.Decision("fill", D("100.05"), D(5))
    assert engine.decide_placement(spec(side="sell"), q, True, D(5)).price == D("99.95")
    assert engine.decide_placement(spec(), q, False, D(5)).action == "queue"
    assert engine.decide_placement(spec(), q, False, D(5), "reject") == engine.Decision(
        "reject", reason="market_closed"
    )
    assert engine.decide_placement(spec("limit", limit="105"), q, True, D(5)) == engine.Decision(
        "fill", D("105"), D(0)
    )
    assert engine.decide_placement(spec("limit", limit="95"), q, True, D(5)).action == "work"
    assert engine.decide_placement(spec("limit", limit="95"), q, False, D(5)).action == "queue"
    assert engine.decide_placement(spec("limit", limit="95"), q, False, D(5), "reject").action == "queue"


def test_resting_decisions() -> None:
    q = quote("100")
    assert engine.decide_resting(spec(), "queued", q, True, D(5)).action == "fill"
    assert engine.decide_resting(spec(), "queued", quote("100", "stale"), True, D(5)).action == "queue"
    assert engine.decide_resting(spec(), "queued", None, True, D(5)).action == "queue"
    assert engine.decide_resting(spec(), "queued", q, False, D(5)).action == "queue"
    assert engine.decide_resting(spec("limit", limit="95"), "working", q, False, D(5)) == engine.Decision(
        "reject", reason="expired"
    )
    assert engine.decide_resting(spec("limit", limit="95"), "queued", q, True, D(5)).action == "work"
    assert engine.decide_resting(spec("limit", limit="95"), "working", None, True, D(5)).action == "work"
    assert engine.decide_resting(spec("limit", limit="105"), "working", q, True, D(5)) == engine.Decision(
        "fill", D("105"), D(0)
    )
    # a stale quote does not stop a limit order that has crossed
    assert (
        engine.decide_resting(spec("limit", limit="105"), "working", quote("100", "stale"), True, D(5)).action
        == "fill"
    )


def test_estimated_price_is_the_limit_for_limits() -> None:
    assert engine.estimated_price(spec("limit", limit="97"), quote("100"), D(5)) == D("97")
    assert engine.estimated_price(spec(), quote("100"), D(5)) == D("100.05")


def test_check_assembly_orders_rows_like_the_design() -> None:
    rules = Rules(D(50), D(80), D(20), 9, D(0))
    pf = Portfolio(D(10_000), (Position("X", "T", D(10), D(100)),))
    risk = validate(TradeProposal("X", "buy", D(10), D(100), "T"), pf, rules)
    s = spec()
    checks = engine.assemble_checks(risk, s, quote(), False, "queue", D("100.05"), D(10_000), D(0))
    assert [c.key for c in checks] == ["funds", "pos", "sec", "trades", "session"]
    summary = engine.summarize(checks)
    assert not summary.blocked and not summary.hard and not summary.rule
    stale = engine.assemble_checks(
        risk, s, quote(freshness="stale"), True, "queue", D("100.05"), D(10_000), D(0)
    )
    assert stale[-1].key == "freshness" and engine.summarize(stale).hard
    bad_limit = engine.assemble_checks(
        risk, engine.OrderSpec("X", "buy", "limit", D(1), None), quote(), True, "queue", None, D(1), D(0)
    )
    assert bad_limit[0].key == "limit" and bad_limit[0].detail == "Enter a limit price above $0"
    reject = engine.assemble_checks(risk, s, quote(), False, "reject", D("100.05"), D(10_000), D(0))
    assert reject[-1].key == "session_reject" and engine.summarize(reject).hard
    # a fee makes the cash check stricter
    tight = engine.assemble_checks(risk, s, quote(), True, "queue", D("100"), D(1000), D("0.01"))
    assert next(c for c in tight if c.key == "funds").ok is False


def test_summary_separates_overridable_rule_breaches_from_hard_failures() -> None:
    rules = Rules(D(5), D(80), D(20), 9, D(0))
    pf = Portfolio(D(1000), (Position("X", "T", D(1), D(100)),))
    risk = validate(TradeProposal("X", "buy", D(5), D(100), "T"), pf, rules)
    five = engine.OrderSpec("X", "buy", "market", D(5))
    checks = engine.assemble_checks(risk, five, quote(), True, "queue", D("100.05"), D(1000), D(0))
    s = engine.summarize(checks)
    assert s.blocked and [c.key for c in s.rule] == ["pos"] and not s.hard
    assert [v.rule for v in engine.violations_for(risk, s.rule)] == ["pos"]


# ---------------------------------------------------------------------------------------- ledger arithmetic


def test_average_cost_buy_sell() -> None:
    lot = apply_buy(None, D(10), D(100))
    lot = apply_buy(lot, D(10), D(110))
    assert lot == Lot(D(20), D("105.0000"))
    rest, pnl = apply_sell(lot, D(5), D(120))
    assert rest == Lot(D(15), D("105.0000")) and pnl == D(75)
    flat, _ = apply_sell(rest, D(15), D(90))
    assert flat is None


@pytest.mark.parametrize("qty", [0, -1])
def test_non_positive_quantities_rejected(qty: int) -> None:
    with pytest.raises(LedgerError):
        apply_buy(None, D(qty), D(1))
    with pytest.raises(LedgerError):
        apply_sell(Lot(D(1), D(1)), D(qty), D(1))


def test_no_shorting_in_the_ledger() -> None:
    with pytest.raises(LedgerError, match="no shorting"):
        apply_sell(None, D(1), D(1))
    with pytest.raises(LedgerError):
        apply_sell(Lot(D(1), D(1)), D(2), D(1))
    with pytest.raises(LedgerError):
        apply_split(Lot(D(1), D(1)), D(0))


def test_split_preserves_value() -> None:
    lot = Lot(D(24), D("231.5"))
    s = apply_split(lot, D(4))
    assert s.qty == 96 and s.avg_cost == D("57.8750")
    assert s.qty * D("67.175") == lot.qty * D("268.70")


def test_rebuild_applies_splits_by_time() -> None:
    pre = FillRecord("X", "buy", D(10), D(100), datetime(2026, 1, 1, tzinfo=UTC))
    post = FillRecord("X", "buy", D(10), D(30), datetime(2026, 3, 1, tzinfo=UTC))
    sp = SplitRecord("X", date(2026, 2, 1), D(4))
    book = rebuild_positions([post, pre], [sp])  # order of input is irrelevant
    assert book["X"].qty == 50
    assert book["X"].avg_cost == ((D("25") * 40 + D(30) * 10) / 50).quantize(D("0.0001"))
    assert split_effective_at(date(2026, 2, 1)).hour == 0
    # a split for a symbol that was not held yet changes nothing
    assert rebuild_positions([post], [sp])["X"] == Lot(D(10), D(30))
    with pytest.raises(LedgerError):
        rebuild_positions([FillRecord("X", "sell", D(1), D(1), T0)])


def test_cash_effect_signs_and_fee() -> None:
    assert cash_effect("buy", D(2), D(10)) == -20
    assert cash_effect("sell", D(2), D(10), D(1)) == 19
    assert q2(D("1.005")) == D("1.01")


def test_solve_opening_lot_hits_the_target_exactly() -> None:
    later: list[tuple[Side, Decimal, Decimal]] = [("buy", D(12), D("168.3"))]
    q0, p0 = solve_opening_lot(later, D(62), D("152.4"))
    lot = apply_buy(Lot(q0, p0), D(12), D("168.3"))
    assert q0 == 50 and lot == Lot(D(62), D("152.4000"))
    # no later fills: the opening lot IS the position
    assert solve_opening_lot([], D(10), D("12.3456")) == (D(10), D("12.3456"))


def test_solve_opening_lot_failure_modes() -> None:
    with pytest.raises(LedgerError, match="exceed"):
        solve_opening_lot([("buy", D(10), D(1))], D(5), D(1))
    with pytest.raises(LedgerError, match="unreachable"):
        solve_opening_lot([("buy", D(10), D(500))], D(20), D(1))
    with pytest.raises(LedgerError, match="oversell"):
        solve_opening_lot([("sell", D(60), D(1)), ("buy", D(100), D(1))], D(90), D(1))


fill_strategy = st.lists(
    st.tuples(
        st.sampled_from(["A", "B", "C"]),
        st.sampled_from(["buy", "sell"]),
        st.integers(1, 50),
        st.decimals(min_value="1", max_value="500", places=2),
    ),
    min_size=1,
    max_size=40,
)


def _legalise(raw: list[tuple[str, str, int, Decimal]]) -> list[FillRecord]:
    """Drop sells that would oversell so every sequence is a valid ledger history."""
    held: dict[str, Decimal] = {}
    out: list[FillRecord] = []
    for i, (sym, side, qty, px) in enumerate(raw):
        q = D(qty)
        if side == "sell":
            if held.get(sym, D(0)) < q:
                continue
            held[sym] -= q
        else:
            held[sym] = held.get(sym, D(0)) + q
        out.append(FillRecord(sym, side, q, px, T0 + timedelta(minutes=i)))  # type: ignore[arg-type]
    return out


@given(fill_strategy)
@PROP
def test_rebuild_equals_incremental_and_quantities_are_conserved(
    raw: list[tuple[str, str, int, Decimal]],
) -> None:
    fills = _legalise(raw)
    book: dict[str, Lot] = {}
    for f in fills:
        if f.side == "buy":
            book[f.symbol] = apply_buy(book.get(f.symbol), f.qty, f.price)
        else:
            rest, _ = apply_sell(book.get(f.symbol), f.qty, f.price)
            if rest is None:
                book.pop(f.symbol)
            else:
                book[f.symbol] = rest
    assert rebuild_positions(fills) == book
    for sym in {f.symbol for f in fills}:
        net = sum((f.qty if f.side == "buy" else -f.qty for f in fills if f.symbol == sym), D(0))
        assert book.get(sym, Lot(D(0), D(0))).qty == net >= 0


@given(fill_strategy, st.decimals(min_value="1", max_value="500", places=2))
@PROP
def test_equity_is_conserved_cash_plus_marked_positions(
    raw: list[tuple[str, str, int, Decimal]], mark: Decimal
) -> None:
    """cash + sum(qty x last) == starting cash + realised + unrealised (to the cent of average-cost rounding)."""
    fills = _legalise(raw)
    start = D(1_000_000)
    cash = start + sum((cash_effect(f.side, f.qty, f.price) for f in fills), D(0))
    book = rebuild_positions(fills)
    value = sum((lot.qty * mark for lot in book.values()), D(0))
    realised = sum((p for _, p in realised_pnl_of(fills)), D(0))
    unrealised = sum((lot.qty * (mark - lot.avg_cost) for lot in book.values()), D(0))
    assert cash + value == pytest.approx(start + realised + unrealised, abs=D("0.5") * (len(fills) + 1))


@given(fill_strategy, st.integers(2, 10))
@PROP
def test_split_never_changes_value_or_unrealised_pnl(
    raw: list[tuple[str, str, int, Decimal]], ratio: int
) -> None:
    fills = _legalise(raw)
    book = rebuild_positions(fills)
    for lot in book.values():
        s = apply_split(lot, D(ratio))
        assert s.qty == lot.qty * ratio
        px = D("123.45")
        assert s.qty * (px / ratio) == pytest.approx(lot.qty * px, abs=D("0.0001"))
        assert s.qty * (px / ratio - s.avg_cost) == pytest.approx(
            lot.qty * (px - lot.avg_cost), abs=D("0.05") * lot.qty
        )
