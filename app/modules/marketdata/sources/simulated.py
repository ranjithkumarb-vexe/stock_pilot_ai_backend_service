"""Seeded, deterministic random-walk price source (clearly labelled `source: "simulated"`).

Each symbol follows a mean-reverting log random walk (Ornstein-Uhlenbeck) around an *anchor* - the last known
close - so a long-running demo drifts around real-looking levels instead of wandering off. The walk of a
symbol depends only on `(seed, symbol, UTC date)` and the number of steps taken, so replays are exact.

Not market data: never presented as real.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from app.core.clock import Clock
from app.core.pricesource import Quote, QuoteUnavailableError

SOURCE_LABEL = "simulated"

# Illustrative last closes (from the design mock, NOT market data) used only when `prices_daily` has no row.
DEFAULT_ANCHORS: dict[str, Decimal] = {
    s: Decimal(p)
    for s, p in {
        "NVDA": "182.40",
        "AMD": "164.20",
        "TSLA": "342.10",
        "META": "718.50",
        "AMZN": "228.90",
        "AVGO": "352.30",
        "GOOGL": "281.40",
        "AAPL": "268.70",
        "MSFT": "521.60",
        "JNJ": "171.80",
        "KO": "69.40",
        "PG": "156.30",
        "PEP": "148.60",
        "VZ": "41.90",
        "XOM": "114.20",
        "JPM": "301.50",
        "SPY": "680.00",
    }.items()
}

_CENT = Decimal("0.01")
_HALF_SPREAD = 0.00005  # 0.5 bp each side


def fallback_anchor(symbol: str) -> Decimal:
    """Deterministic made-up level for symbols with neither a DB close nor a design price."""
    h = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    return Decimal(30 + (h % 37000) / 100).quantize(_CENT)


def money(x: float) -> Decimal:
    return Decimal(repr(x)).quantize(_CENT, rounding=ROUND_HALF_EVEN)


@dataclass(slots=True)
class _SymbolState:
    anchor: float
    rng: random.Random
    log_dev: float  # ln(price / anchor)
    day: date
    steps: int = 0
    price: float = field(init=False)

    def __post_init__(self) -> None:
        self.price = self.anchor * math.exp(self.log_dev)


class SimulatedSource:
    name = SOURCE_LABEL

    def __init__(
        self,
        anchors: Mapping[str, Decimal] | None,
        clock: Clock,
        *,
        seed: int = 42,
        sigma: float = 0.0004,
        theta: float = 0.002,
        tick_s: float = 1.0,
    ) -> None:
        self._anchors: dict[str, Decimal] = dict(anchors or {})
        self._clock = clock
        self._seed = seed
        self._sigma = sigma
        self._theta = theta
        self.tick_s = tick_s
        self._state: dict[str, _SymbolState] = {}

    # -- anchors -------------------------------------------------------
    def set_anchor(self, symbol: str, price: Decimal) -> None:
        self._anchors[symbol] = price
        self._state.pop(symbol, None)

    def anchor(self, symbol: str) -> Decimal:
        found = self._anchors.get(symbol) or DEFAULT_ANCHORS.get(symbol)
        return found if found is not None else fallback_anchor(symbol)

    # -- walk ----------------------------------------------------------
    def _get_state(self, symbol: str) -> _SymbolState:
        today = self._clock.now().date()
        st = self._state.get(symbol)
        if st is None or st.day != today:
            # per-(seed, symbol, day) stream; the first draw is the opening gap vs the anchor
            rng = random.Random(f"{self._seed}:{symbol}:{today.isoformat()}")  # noqa: S311 - not security
            st = _SymbolState(
                anchor=float(self.anchor(symbol)),
                rng=rng,
                log_dev=rng.gauss(0.0, 0.006),
                day=today,
            )
            self._state[symbol] = st
        return st

    def _advance(self, st: _SymbolState) -> None:
        st.log_dev += -self._theta * st.log_dev + self._sigma * st.rng.gauss(0.0, 1.0)
        st.price = st.anchor * math.exp(st.log_dev)
        st.steps += 1

    def _quote(self, symbol: str, st: _SymbolState) -> Quote:
        price = money(st.price)
        return Quote(
            symbol=symbol,
            price=price,
            ts=self._clock.now(),
            source=SOURCE_LABEL,
            prev_close=money(st.anchor),
            bid=money(st.price * (1 - _HALF_SPREAD)),
            ask=money(st.price * (1 + _HALF_SPREAD)),
        )

    def step(self, symbols: Iterable[str]) -> list[Quote]:
        """Advance every symbol by one tick and return the new quotes."""
        out: list[Quote] = []
        for symbol in symbols:
            st = self._get_state(symbol)
            self._advance(st)
            out.append(self._quote(symbol, st))
        return out

    def current(self, symbol: str) -> Quote:
        """The quote at the present state (no tick). Used to seed the store at startup."""
        return self._quote(symbol, self._get_state(symbol))

    # -- PriceSource ---------------------------------------------------
    async def get_last(self, symbol: str) -> Quote:
        if not symbol or not symbol.isascii():
            raise QuoteUnavailableError(symbol)
        return self.current(symbol)

    async def subscribe(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        syms: Sequence[str] = list(symbols)
        while True:
            for q in self.step(syms):
                yield q
            await asyncio.sleep(self.tick_s)
