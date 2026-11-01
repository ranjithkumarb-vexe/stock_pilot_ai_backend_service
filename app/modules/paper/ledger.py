"""Pure ledger arithmetic for the paper engine (no I/O, no clock).

Accounting rules (SPEC 6.5):
* positions use the AVERAGE-COST method: a buy re-weights the average, a sell leaves it unchanged;
* `fills` and `cash_ledger` are append-only, `positions` is derived and always rebuildable from fills
  (+ corporate actions) with `rebuild_positions`;
* every step rounds the average cost to 4 dp (the column precision), so the incremental update done at fill
  time and a rebuild from scratch give bit-identical results.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

Side = Literal["buy", "sell"]
ET = ZoneInfo("America/New_York")
ZERO = Decimal(0)
_Q4 = Decimal("0.0001")
_Q2 = Decimal("0.01")


def q4(value: Decimal) -> Decimal:
    return value.quantize(_Q4, rounding=ROUND_HALF_UP)


def q2(value: Decimal) -> Decimal:
    return value.quantize(_Q2, rounding=ROUND_HALF_UP)


class LedgerError(ValueError):
    """An operation that would break an accounting invariant (e.g. selling more than held)."""


@dataclass(frozen=True, slots=True)
class Lot:
    qty: Decimal
    avg_cost: Decimal


@dataclass(frozen=True, slots=True)
class FillRecord:
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    filled_at: datetime
    fee: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class SplitRecord:
    symbol: str
    ex_date: date
    ratio: Decimal  # 4 for a 4-for-1 split


def apply_buy(lot: Lot | None, qty: Decimal, price: Decimal) -> Lot:
    if qty <= 0:
        raise LedgerError("quantity must be positive")
    if lot is None or lot.qty == 0:
        return Lot(qty, q4(price))
    total = lot.qty + qty
    return Lot(total, q4((lot.avg_cost * lot.qty + price * qty) / total))


def apply_sell(lot: Lot | None, qty: Decimal, price: Decimal) -> tuple[Lot | None, Decimal]:
    """Returns the remaining lot (None when flat) and the realised P&L of this sale."""
    if qty <= 0:
        raise LedgerError("quantity must be positive")
    held = lot.qty if lot else ZERO
    if lot is None or qty > held:
        raise LedgerError(f"cannot sell {qty}; {held} held (no shorting)")
    realised = (price - lot.avg_cost) * qty
    remaining = lot.qty - qty
    return (None if remaining == 0 else Lot(remaining, lot.avg_cost)), realised


def apply_split(lot: Lot, ratio: Decimal) -> Lot:
    """Qty x ratio, cost basis / ratio: market value (and therefore P&L) is unchanged."""
    if ratio <= 0:
        raise LedgerError("split ratio must be positive")
    return Lot(lot.qty * ratio, q4(lot.avg_cost / ratio))


def cash_effect(side: Side, qty: Decimal, price: Decimal, fee: Decimal = ZERO) -> Decimal:
    """Signed change of cash for one fill (buy: negative)."""
    gross = qty * price
    return (-gross if side == "buy" else gross) - fee


def split_effective_at(ex_date: date) -> datetime:
    """A split is effective from the start of its ex-date in exchange (New York) time."""
    return datetime(ex_date.year, ex_date.month, ex_date.day, tzinfo=ET)


def rebuild_positions(fills: Iterable[FillRecord], splits: Sequence[SplitRecord] = ()) -> dict[str, Lot]:
    """Replay fills (+ splits) in time order. Raises `LedgerError` on an oversell.

    Splits only affect symbols already held when they take effect; fills at/after the effective instant are
    in post-split share terms already.
    """
    events: list[tuple[datetime, int, FillRecord | SplitRecord]] = []
    for f in fills:
        events.append((f.filled_at, 1, f))
    for s in splits:
        events.append((split_effective_at(s.ex_date), 0, s))
    events.sort(key=lambda e: (e[0], e[1]))
    book: dict[str, Lot] = {}
    for _, _, ev in events:
        if isinstance(ev, SplitRecord):
            held = book.get(ev.symbol)
            if held is not None:
                book[ev.symbol] = apply_split(held, ev.ratio)
        elif ev.side == "buy":
            book[ev.symbol] = apply_buy(book.get(ev.symbol), ev.qty, ev.price)
        else:
            remaining, _ = apply_sell(book.get(ev.symbol), ev.qty, ev.price)
            if remaining is None:
                book.pop(ev.symbol, None)
            else:
                book[ev.symbol] = remaining
    return book


def realised_pnl_of(fills: Iterable[FillRecord]) -> list[tuple[FillRecord, Decimal]]:
    """Realised P&L per sell fill (average-cost basis), in time order. Splits are ignored (P&L neutral)."""
    book: dict[str, Lot] = {}
    out: list[tuple[FillRecord, Decimal]] = []
    for f in sorted(fills, key=lambda x: x.filled_at):
        if f.side == "buy":
            book[f.symbol] = apply_buy(book.get(f.symbol), f.qty, f.price)
        else:
            remaining, pnl = apply_sell(book.get(f.symbol), f.qty, f.price)
            out.append((f, pnl - f.fee))
            if remaining is None:
                book.pop(f.symbol, None)
            else:
                book[f.symbol] = remaining
    return out


def solve_opening_lot(
    later: Sequence[tuple[Side, Decimal, Decimal]], final_qty: Decimal, final_avg: Decimal
) -> tuple[Decimal, Decimal]:
    """Find the opening buy `(qty, price)` that, followed by `later` (side, qty, price) fills in order, leaves
    exactly `final_qty` shares at `final_avg` (4 dp). Used by the seed to reproduce the design's holdings
    through real fills. Raises `LedgerError` when no non-negative opening position can do it.
    """
    net = sum((q if s == "buy" else -q for s, q, _ in later), ZERO)
    q0 = final_qty - net
    if q0 <= 0:
        raise LedgerError("later fills already exceed the target position")

    def run(p0: Decimal, exact: bool = False) -> Decimal:
        rnd = (lambda x: x) if exact else q4
        qty, avg = q0, rnd(p0)
        for side, q, p in later:
            if side == "buy":
                avg = rnd((avg * qty + p * q) / (qty + q))
                qty += q
            else:
                if q > qty:
                    raise LedgerError("later fills oversell the opening position")
                qty -= q
        return avg

    # the un-rounded recursion is linear in p0: two probes give the exact solution; the rounded recursion is
    # then searched around it for the 4 dp opening price that lands exactly on `final_avg`
    a0, a1 = run(Decimal(0), True), run(Decimal(1), True)
    slope = a1 - a0
    if slope == 0:
        raise LedgerError("opening price does not influence the final average cost")
    p_exact = (final_avg - a0) / slope
    if p_exact <= 0:
        raise LedgerError("target cost basis unreachable with a positive opening price")
    base = q4(p_exact)
    best = base
    best_err = abs(run(base) - final_avg)
    for k in range(-30, 31):
        cand = base + k * _Q4
        if cand <= 0:
            continue
        err = abs(run(cand) - final_avg)
        if err < best_err:
            best, best_err = cand, err
            if err == 0:
                break
    return q0, best
