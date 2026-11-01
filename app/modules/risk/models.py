"""Stable value types of the risk engine (frozen dataclasses; the public API of `app.modules.risk`).

Units: money and prices are `Decimal`; weights inside the engine are FRACTIONS (0.15 == 15 %); everything
that crosses an API boundary as `*_pct` is a percent number. Rule limits are percent numbers like the CONTRACT
`Rules object` (`pos 10` == 10 %), `trades` is an integer per rolling 7 days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

Side = Literal["buy", "sell"]
RuleKey = Literal["pos", "sec", "dd", "trades", "cash"]
RULE_KEYS: tuple[RuleKey, ...] = ("pos", "sec", "dd", "trades", "cash")
Severity = Literal["pass", "note", "blocked"]


@dataclass(frozen=True, slots=True)
class Rules:
    """User risk rules (hard veto). `pos`/`sec`/`dd`/`cash` are percents, `trades` per rolling 7 days."""

    pos: Decimal
    sec: Decimal
    dd: Decimal
    trades: int
    cash: Decimal

    @classmethod
    def from_mapping(cls, m: dict[str, Any]) -> Rules:
        return cls(
            pos=Decimal(str(m["pos"])),
            sec=Decimal(str(m["sec"])),
            dd=Decimal(str(m["dd"])),
            trades=int(Decimal(str(m["trades"]))),
            cash=Decimal(str(m["cash"])),
        )

    def to_dict(self) -> dict[str, float | int]:
        """CONTRACT `Rules object`: `{pos, sec, dd, trades, cash}` as JSON numbers."""
        return {
            "pos": float(self.pos),
            "sec": float(self.sec),
            "dd": float(self.dd),
            "trades": self.trades,
            "cash": float(self.cash),
        }


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    sector: str | None
    qty: Decimal
    price: Decimal  # latest price
    prev_close: Decimal | None = None
    avg_cost: Decimal | None = None
    name: str = ""

    @property
    def value(self) -> Decimal:
        return self.qty * self.price


@dataclass(frozen=True, slots=True)
class Portfolio:
    cash: Decimal
    positions: tuple[Position, ...] = ()
    trades_7d: int = 0

    @property
    def invested(self) -> Decimal:
        return sum((p.value for p in self.positions), Decimal(0))

    @property
    def equity(self) -> Decimal:
        return self.cash + self.invested


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """What `validate` judges. `price` is the price used to size the trade (slippage is the paper engine's job)."""

    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    sector: str | None = None  # looked up from the portfolio when None and the symbol is already held


@dataclass(frozen=True, slots=True)
class Violation:
    """A rule that would be (or is) broken. `hard` = never overridable (no shorting, no cash, bad quantity)."""

    rule: str  # pos | sec | dd | trades | cash | shares | funds | quantity
    label: str
    message: str
    limit: Decimal | None = None
    value: Decimal | None = None
    hard: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "label": self.label,
            "message": self.message,
            "limit": None if self.limit is None else float(self.limit),
            "value": None if self.value is None else float(self.value),
            "hard": self.hard,
        }


@dataclass(frozen=True, slots=True)
class RuleCheck:
    """One line of the pre-trade checklist; wording identical in meaning to the design's `check()`."""

    key: str
    ok: bool
    severity: Severity  # pass | note | blocked
    label: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "ok": self.ok,
            "severity": self.severity,
            "label": self.label,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    allowed: bool
    violations: tuple[Violation, ...] = ()
    warnings: tuple[Violation, ...] = ()
    checks: tuple[RuleCheck, ...] = field(default=())

    @property
    def blocked_hard(self) -> bool:
        return any(v.hard for v in self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": [w.to_dict() for w in self.warnings],
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    equity: Decimal
    cash: Decimal
    invested: Decimal
    cash_weight: Decimal  # fraction of equity
    weights: dict[str, Decimal]  # symbol -> fraction of EQUITY (positions + cash_weight sum to 1)
    invested_weights: dict[str, Decimal]  # symbol -> fraction of INVESTED capital (sums to 1)
    sector_weights: dict[str, Decimal]  # sector -> fraction of equity
    hhi: float | None  # sum of squared invested weights; None without positions
    effective_n: float | None  # 1 / hhi
    max_position: tuple[str, Decimal] | None
    max_sector: tuple[str, Decimal] | None
    drawdown_pct: float | None  # current drawdown from the curve peak, percent (>= 0)
    max_drawdown_pct: float | None
    vol_60d: float | None  # annualised, percent
    beta: float | None  # vs benchmark, 60-252 observation window
    trades_7d: int
    day_change: Decimal  # sum(qty * (price - prev_close)) over positions with a prev_close
    flags: tuple[str, ...] = ()  # data-quality flags: insufficient_history, zero_variance, ...

    def to_dict(self) -> dict[str, Any]:
        return {
            "hhi": self.hhi,
            "effective_n": self.effective_n,
            "vol_60d": self.vol_60d,
            "beta": self.beta,
            "max_drawdown_pct": self.max_drawdown_pct,
            "drawdown_pct": self.drawdown_pct,
            "trades_7d": self.trades_7d,
            "flags": list(self.flags),
        }


RuleState = Literal["ok", "breach", "warn"]


@dataclass(frozen=True, slots=True)
class RuleStatus:
    key: RuleKey
    limit: Decimal
    current: Decimal
    status: RuleState
    subject: str | None = None  # symbol / sector that is the worst offender, when applicable

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "limit": float(self.limit),
            "current": float(self.current),
            "status": self.status,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class CurveSummary:
    """Whole-window statistics of an equity curve vs a benchmark (performance panel)."""

    period_return_pct: float | None
    benchmark_return_pct: float | None
    vol_pct: float | None  # annualised, full window
    beta: float | None  # full window (max 252 observations)
    drawdown_pct: float | None
    max_drawdown_pct: float | None
    flags: tuple[str, ...] = ()
