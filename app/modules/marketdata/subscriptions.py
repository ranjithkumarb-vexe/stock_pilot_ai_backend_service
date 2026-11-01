"""Subscription set = union of all users' watchlists and holdings (SPEC 6.4 rule 1), capped for the WebSocket.

Priority: symbols held by more users first, then more watchers, then alphabetical. Symbols beyond the cap are
not dropped - they are served by REST polling (T-DATA-06) - and a warning is logged.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.core.db import Connection
from app.core.logging import get_logger

log = get_logger("app.marketdata.subscriptions")


@dataclass(frozen=True, slots=True)
class SymbolDemand:
    symbol: str
    watchers: int = 0
    holders: int = 0


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    ws: tuple[str, ...]  # real-time stream
    rest: tuple[str, ...]  # overflow served by REST polling
    total: int

    @property
    def all(self) -> tuple[str, ...]:
        return self.ws + self.rest

    @property
    def overflow(self) -> bool:
        return bool(self.rest)


def plan_subscriptions(
    demand: Iterable[SymbolDemand],
    *,
    cap: int = 30,
    warn_at: int = 25,
    fallback_universe: Iterable[str] = (),
) -> SubscriptionPlan:
    merged: dict[str, SymbolDemand] = {}
    for d in demand:
        cur = merged.get(d.symbol)
        merged[d.symbol] = (
            d if cur is None else SymbolDemand(d.symbol, cur.watchers + d.watchers, cur.holders + d.holders)
        )
    if not merged:  # nobody watches anything yet: stream the universe so the app still has prices
        merged = {s: SymbolDemand(s) for s in fallback_universe}
    ordered = sorted(merged.values(), key=lambda d: (-d.holders, -d.watchers, d.symbol))
    symbols = [d.symbol for d in ordered]
    total = len(symbols)
    if total > cap:
        log.warning(
            "subscription set exceeds websocket cap; overflow served via REST",
            extra={"total": total, "cap": cap, "overflow": total - cap},
        )
    elif total > warn_at:
        log.warning("subscription set approaching websocket cap", extra={"total": total, "cap": cap})
    return SubscriptionPlan(ws=tuple(symbols[:cap]), rest=tuple(symbols[cap:]), total=total)


async def load_demand(conn: Connection) -> list[SymbolDemand]:
    """Aggregate demand through the SECURITY DEFINER function (counts only, no user ids)."""
    rows = await conn.fetch("select symbol, watchers, holders from sp_subscription_symbols()")
    return [SymbolDemand(r["symbol"], int(r["watchers"]), int(r["holders"])) for r in rows]


async def load_universe(conn: Connection) -> list[str]:
    rows = await conn.fetch("select symbol from tickers where in_universe order by symbol")
    return [r["symbol"] for r in rows]
