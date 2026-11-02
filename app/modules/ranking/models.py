"""Value types of the ranker."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

Kind = Literal["alert", "proposal", "briefing"]
ARMS: tuple[str, ...] = ("breach", "proposal", "filing", "move", "earnings", "mirror", "brief")
SECTOR_ARM = "sector"  # shared across item types: Beta(sector preference) per `context = "sector:<S>"`
ALERT_TYPE_ARM: dict[str, str] = {
    "breach": "breach",
    "concentration": "breach",
    "filing": "filing",
    "move": "move",
    "earnings": "earnings",
    "mirror": "mirror",
}
ARM_LABEL: dict[str, str] = {
    "breach": "rule breach",
    "proposal": "proposal",
    "filing": "8-K filing",
    "move": "unusual move",
    "earnings": "earnings",
    "mirror": "behaviour mirror",
    "brief": "research brief",
}


@dataclass(frozen=True, slots=True)
class Arm:
    """Beta(alpha, beta) posterior of one bandit arm (bounded, see `scoring.clamp_arm`)."""

    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


@dataclass(frozen=True, slots=True)
class TradeSpec:
    """A trade a candidate would lead to; the risk engine must allow it before the item is ever ranked."""

    symbol: str
    side: Literal["buy", "sell"]
    qty: Decimal
    sector: str | None = None


def dedupe_rules(rules: Iterable[Any]) -> list[dict[str, Any]]:
    """`[{rule, passed}]` with each rule label once (first position; a failed check wins over a passed one)."""
    out: dict[str, dict[str, Any]] = {}
    for r in rules:
        if not isinstance(r, dict):
            continue
        label, passed = str(r.get("rule", "")), bool(r.get("passed", True))
        if label in out:
            out[label]["passed"] = out[label]["passed"] and passed
        else:
            out[label] = {"rule": label, "passed": passed}
    return list(out.values())


@dataclass(slots=True)
class Candidate:
    kind: Kind
    id: str
    arm: str
    materiality: float
    critical: bool
    title: str
    created_at: datetime
    item: dict[str, Any]
    symbol: str | None = None
    sector: str | None = None
    sources: list[str] = field(default_factory=list)
    rules_checked: list[dict[str, Any]] = field(default_factory=list)
    trade: TradeSpec | None = None

    @property
    def interest_dim(self) -> str | None:
        return f"sector:{self.sector}" if self.sector else None

    @property
    def context(self) -> str:
        """Bandit context of this item (sector bucket); '' = type-level arm only."""
        return f"sector:{self.sector}" if self.sector else ""


@dataclass(slots=True)
class Scored:
    cand: Candidate
    score: float
    materiality: float
    thompson: float  # effective draw (after the epsilon floor)
    interest: float
    pinned: bool
    arm_mean: float
    explored: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RankResult:
    items: list[Scored]
    dropped: list[tuple[Candidate, str]] = field(default_factory=list)
