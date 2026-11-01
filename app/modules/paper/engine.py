"""Pure paper-trading decisions (SPEC 6.5): no I/O, no clock, no database.

Everything here takes plain values and returns plain values, so it is exercised exhaustively by unit and
property tests; `service.py` wires it to the ledger tables. Execution rules:

* market order, session open  -> fill at `last x (1 +- slippage_bps/10000)` (buy up, sell down; 2 dp, as the design)
* market order, session closed-> `queue` (filled at the next open +- slippage) or reject (portfolio setting)
* limit order                 -> fills when `last` crosses the limit (buy: last <= limit, sell: last >= limit)
                                 AT THE LIMIT (no price improvement); otherwise rests (`work`); a limit order
                                 placed while closed is queued and starts working at the open
* day orders that are still working when the session closes expire
* a stale quote (`freshness == "stale"`) blocks MARKET orders (T-DATA-02); limit orders may still rest
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from app.core.pricesource import Quote
from app.modules.risk import RuleCheck, ValidationResult, Violation

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
Action = Literal["fill", "queue", "work", "reject"]

BPS = Decimal(10_000)
_Q2 = Decimal("0.01")

# check keys whose failure is never overridable
HARD_KEYS = frozenset({"quantity", "shares", "funds", "limit", "freshness", "session_reject", "symbol"})
# rule keys the user may knowingly override (logged as an event, never loosens a rule)
RULE_KEYS = frozenset({"pos", "sec", "cash", "trades", "dd"})


def slipped_price(last: Decimal, side: Side, slippage_bps: Decimal) -> Decimal:
    """Design: buy x 1.0005, sell x 0.9995, rounded to cents."""
    factor = Decimal(1) + (slippage_bps / BPS if side == "buy" else -slippage_bps / BPS)
    return (last * factor).quantize(_Q2, rounding=ROUND_HALF_UP)


def limit_crosses(side: Side, last: Decimal, limit: Decimal) -> bool:
    return last <= limit if side == "buy" else last >= limit


@dataclass(frozen=True, slots=True)
class OrderSpec:
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal
    limit_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    price: Decimal | None = None  # execution price when action == "fill"
    slippage_bps: Decimal = Decimal(0)  # applied slippage (0 for limit fills)
    reason: str | None = None


def decide_placement(
    spec: OrderSpec, quote: Quote, market_open: bool, slippage_bps: Decimal, after_hours_policy: str = "queue"
) -> Decision:
    """What happens to a NEW order (checks have already passed)."""
    if spec.type == "market":
        if not market_open:
            if after_hours_policy == "reject":
                return Decision("reject", reason="market_closed")
            return Decision("queue")
        return Decision("fill", slipped_price(quote.price, spec.side, slippage_bps), slippage_bps)
    assert spec.limit_price is not None
    if not market_open:
        return Decision("queue")
    if limit_crosses(spec.side, quote.price, spec.limit_price):
        return Decision("fill", spec.limit_price, Decimal(0))
    return Decision("work")


def decide_resting(
    spec: OrderSpec, status: str, quote: Quote | None, market_open: bool, slippage_bps: Decimal
) -> Decision:
    """What the sweeper does with a resting order (`queued` or `working`) right now."""
    if not market_open:
        # day limit orders die with the session; queued orders keep waiting for the next open
        return Decision("reject", reason="expired") if status == "working" else Decision("queue")
    if quote is None:
        return Decision("queue" if status == "queued" else "work")
    if spec.type == "market":
        if quote.freshness == "stale":
            return Decision("queue")  # wait for a usable quote; never fill on stale data
        return Decision("fill", slipped_price(quote.price, spec.side, slippage_bps), slippage_bps)
    assert spec.limit_price is not None
    if limit_crosses(spec.side, quote.price, spec.limit_price):
        return Decision("fill", spec.limit_price, Decimal(0))
    return Decision("work")


# --------------------------------------------------------------------------------------------------
# checks (design `check()` rows + the engine's own session / freshness / limit rows)
# --------------------------------------------------------------------------------------------------


def session_check(market_open: bool, spec: OrderSpec, after_hours_policy: str) -> RuleCheck | None:
    if market_open:
        return None
    if spec.type == "market" and after_hours_policy == "reject":
        return RuleCheck(
            "session_reject", False, "blocked", "Market session",
            "Market is closed and your setting rejects after-hours market orders.",
        )  # fmt: skip
    return RuleCheck(
        "session",
        False,
        "note",
        "Market session",
        "Market is closed. The order will be queued for the next open.",
    )


def freshness_check(quote: Quote | None, market_open: bool, spec: OrderSpec) -> RuleCheck | None:
    if quote is not None and quote.freshness == "stale" and market_open and spec.type == "market":
        return RuleCheck(
            "freshness",
            False,
            "blocked",
            "Quote freshness",
            "Quote is older than 15 minutes. Market orders are blocked.",
        )
    return None


def limit_check(spec: OrderSpec) -> RuleCheck | None:
    if spec.type == "limit" and not (spec.limit_price is not None and spec.limit_price > 0):
        return RuleCheck("limit", False, "blocked", "Limit price", "Enter a limit price above $0")
    return None


def with_slippage_buffer(
    checks: list[RuleCheck], spec: OrderSpec, est_price: Decimal, cash: Decimal, fee: Decimal
) -> list[RuleCheck]:
    """Buys must afford `qty x worst-case price + fee`, not just `qty x last` (SPEC 6.5: slippage buffer)."""
    if spec.side != "buy":
        return checks
    cost = spec.qty * est_price + fee
    out: list[RuleCheck] = []
    for c in checks:
        if c.key == "funds" and c.ok and cost > cash:
            c = RuleCheck(
                "funds", False, "blocked", "Paper cash",
                f"Cost ${cost.quantize(Decimal(1), rounding=ROUND_HALF_UP):,} including the slippage buffer exceeds ${cash.quantize(Decimal(1), rounding=ROUND_HALF_UP):,} available",
            )  # fmt: skip
        out.append(c)
    return out


def assemble_checks(
    risk: ValidationResult,
    spec: OrderSpec,
    quote: Quote | None,
    market_open: bool,
    after_hours_policy: str,
    est_price: Decimal | None,
    cash: Decimal,
    fee: Decimal,
) -> list[RuleCheck]:
    checks = list(risk.checks)
    lim = limit_check(spec)
    if lim is not None:
        checks.insert(0, lim)
    if est_price is not None:
        checks = with_slippage_buffer(checks, spec, est_price, cash, fee)
    for extra in (
        session_check(market_open, spec, after_hours_policy),
        freshness_check(quote, market_open, spec),
    ):
        if extra is not None:
            checks.append(extra)
    return checks


@dataclass(frozen=True, slots=True)
class CheckSummary:
    blocked: bool  # any 'blocked' row (the UI's "Blocked")
    hard: tuple[RuleCheck, ...]  # never overridable
    rule: tuple[RuleCheck, ...]  # overridable rule breaches


def summarize(checks: list[RuleCheck]) -> CheckSummary:
    failing = [c for c in checks if c.severity == "blocked"]
    hard = tuple(c for c in failing if c.key in HARD_KEYS or c.key not in RULE_KEYS)
    rule = tuple(c for c in failing if c.key in RULE_KEYS)
    return CheckSummary(bool(failing), hard, rule)


def violations_for(risk: ValidationResult, rule_checks: tuple[RuleCheck, ...]) -> list[Violation]:
    keys = {c.key for c in rule_checks}
    return [v for v in risk.violations if v.rule in keys]


# --------------------------------------------------------------------------------------------------
# pricing helper for the cash check
# --------------------------------------------------------------------------------------------------


def estimated_price(spec: OrderSpec, quote: Quote, slippage_bps: Decimal) -> Decimal:
    """Worst-case execution price used for the cash check."""
    if spec.type == "limit" and spec.limit_price is not None:
        return spec.limit_price  # limit orders fill AT the limit
    return slipped_price(quote.price, spec.side, slippage_bps)
