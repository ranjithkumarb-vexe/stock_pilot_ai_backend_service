"""MarketDataService: owns the quote store, the configured upstream source(s), freshness and SSE fan-out.

Exposed to the rest of the app as:
* `app.state.price_source`     - a `PriceSource` (`get_last`, `subscribe`) returning `Quote`s with `session`,
                                 `freshness`, `prev_close`, `bid`/`ask` filled in
* `app.state.market_override`  - `None` (follow the NYSE calendar) | `True` (force open) | `False` (force closed)
* `app.state.feed_override`    - `None` | `live` | `delayed` | `stale` | `reconnecting` (dev "simulate" control)
* `app.state.market_service`   - this object (`session_now()`, `market_open()`, `quote_freshness(symbol)`)
* `app.state.stream_hub`       - `StreamHub.publish_user_event(user_id, "alert"|"proposal", payload)`
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from decimal import Decimal
from typing import Any

import asyncpg

from app.core.clock import Clock
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.pricesource import Freshness, PriceSource, Quote, QuoteUnavailableError, Session
from app.modules.marketdata.calendar import NyseCalendar
from app.modules.marketdata.config import MarketDataConfig
from app.modules.marketdata.freshness import (
    FeedState,
    allows_move_alerts,
    blocks_market_fills,
    compute_freshness,
)
from app.modules.marketdata.hub import StreamHub
from app.modules.marketdata.schemas import QuoteOut
from app.modules.marketdata.sources.base import UpstreamError
from app.modules.marketdata.sources.simulated import SOURCE_LABEL, SimulatedSource
from app.modules.marketdata.store import QuoteStore, StoredQuote
from app.modules.marketdata.subscriptions import (
    SubscriptionPlan,
    SymbolDemand,
    load_demand,
    load_universe,
    plan_subscriptions,
)
from app.modules.marketdata.supervisor import FeedSupervisor

log = get_logger("app.marketdata.service")

_DELAYED_TICK_EVERY = 5  # a "delayed" simulated feed ticks every 5th interval (UI: "updates every 5 s")


class ServicePriceSource:
    """`PriceSource` adapter over the service (what `app.state.price_source` is)."""

    name = "marketdata"

    def __init__(self, service: MarketDataService) -> None:
        self._svc = service

    async def get_last(self, symbol: str) -> Quote:
        q = await self._svc.get_quote(symbol)
        if q is None:
            raise QuoteUnavailableError(symbol)
        return q

    async def subscribe(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        wanted = set(symbols)
        queue: asyncio.Queue[Quote] = asyncio.Queue(maxsize=1000)

        def listener(q: Quote) -> None:
            if q.symbol in wanted and not queue.full():
                queue.put_nowait(q)

        self._svc.add_listener(listener)
        try:
            while True:
                yield await queue.get()
        finally:
            self._svc.remove_listener(listener)


class MarketDataService:
    def __init__(
        self,
        *,
        cfg: MarketDataConfig,
        clock: Clock,
        state: Any,  # app.state: market_override / feed_override live there so other modules can read them
        calendar: NyseCalendar | None = None,
        simulated: SimulatedSource | None = None,
        supervisor: FeedSupervisor | None = None,
        hub: StreamHub | None = None,
        store: QuoteStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self._state = state
        self.calendar = calendar or NyseCalendar()
        self.store = store or QuoteStore()
        self.hub = hub or StreamHub(cfg.ring_size, cfg.subscriber_queue)
        self.simulated = simulated
        self.supervisor = supervisor
        self.price_source: PriceSource = ServicePriceSource(self)
        self._listeners: list[Callable[[Quote], None]] = []
        self._plan = SubscriptionPlan(ws=(), rest=(), total=0)
        self._extra: set[str] = set()
        self._tick_n = 0
        self._tasks: list[asyncio.Task[None]] = []
        self.closers: list[Callable[[], Awaitable[None]]] = []

    # -- overrides (live on app.state so stream A can read them) ------------
    @property
    def market_override(self) -> bool | None:
        value = getattr(self._state, "market_override", None)
        return value if isinstance(value, bool) else None

    @property
    def feed_override(self) -> FeedState | None:
        value = getattr(self._state, "feed_override", None)
        return value if value in ("live", "delayed", "stale", "reconnecting") else None

    def set_market_override(self, value: bool | None) -> None:
        self._state.market_override = value
        self.republish_all()

    def set_feed_override(self, value: FeedState | None) -> None:
        self._state.feed_override = value
        self.republish_all()

    # -- session / freshness ------------------------------------------------
    def session_now(self) -> Session:
        override = self.market_override
        if override is True:
            return "regular"
        if override is False:
            return "closed"
        return self.calendar.session_at(self.clock.now())

    def market_open(self) -> bool:
        return self.session_now() == "regular"

    @property
    def is_simulated(self) -> bool:
        return self.simulated is not None

    @property
    def feed_state(self) -> FeedState:
        override = self.feed_override
        if override is not None:
            return override
        if self.supervisor is not None:
            return self.supervisor.feed_state
        return "live"

    def freshness_of(self, sq: StoredQuote, session: Session | None = None) -> Freshness:
        age = max(0.0, (self.clock.now() - sq.received_at).total_seconds())
        return compute_freshness(
            session=session or self.session_now(),
            kind=sq.kind,
            age_s=age,
            feed_state=self.feed_override,
            live_max_age_s=self.cfg.live_max_age_s,
            stale_after_s=self.cfg.stale_after_s,
        )

    def view(self, sq: StoredQuote) -> Quote:
        session = self.session_now()
        source = sq.source
        if self.supervisor is not None and self.supervisor.tier == "cache":
            source = "cache"
        return Quote(
            symbol=sq.symbol,
            price=sq.price,
            ts=sq.ts,
            source=source,
            prev_close=sq.prev_close,
            bid=sq.bid,
            ask=sq.ask,
            session=session,
            freshness=self.freshness_of(sq, session),
        )

    # -- policy helpers used by paper engine / alerts ---------------------
    def quote_freshness(self, symbol: str) -> Freshness | None:
        sq = self.store.get(symbol)
        return None if sq is None else self.freshness_of(sq)

    def fills_blocked(self, symbol: str) -> bool:
        f = self.quote_freshness(symbol)
        return f is None or blocks_market_fills(f)

    def move_alerts_allowed(self, symbol: str) -> bool:
        f = self.quote_freshness(symbol)
        return f is not None and allows_move_alerts(f)

    # -- ingest / fan-out ------------------------------------------------
    def add_listener(self, fn: Callable[[Quote], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[Quote], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def on_quote(self, sq: StoredQuote) -> None:
        if not self.store.put(sq):
            return
        self._emit(self.store.get(sq.symbol) or sq)

    def _emit(self, sq: StoredQuote) -> None:
        q = self.view(sq)
        self.hub.publish_quote(sq.symbol, QuoteOut.from_quote(q).model_dump(mode="json"))
        for fn in list(self._listeners):
            fn(q)

    def republish_all(self) -> None:
        """After a session/feed override changes: push every quote again with its new session/freshness."""
        for sq in self.store.snapshot():
            self._emit(sq)

    # -- reads -------------------------------------------------------------
    async def get_quote(self, symbol: str) -> Quote | None:
        sq = self.store.get(symbol)
        if sq is None:
            sq = await self._fetch_on_demand(symbol)
        return None if sq is None else self.view(sq)

    async def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        found: dict[str, Quote] = {}
        for s in symbols:
            q = await self.get_quote(s)
            if q is not None:
                found[s] = q
        return found

    async def _fetch_on_demand(self, symbol: str) -> StoredQuote | None:
        self._extra.add(symbol)
        if self.simulated is not None:
            q = self.simulated.current(symbol)
            now = self.clock.now()
            sq = StoredQuote(
                symbol,
                q.price,
                now,
                now,
                SOURCE_LABEL,
                "stream",
                bid=q.bid,
                ask=q.ask,
                prev_close=q.prev_close,
            )
            self.store.put(sq)
            return self.store.get(symbol)
        sup = self.supervisor
        for source in (sup.rest if sup else None, sup.fallback if sup else None):
            if source is None:
                continue
            try:
                got = await source.fetch([symbol])
            except UpstreamError:
                continue
            if symbol in got:
                self.store.put(got[symbol])
                return self.store.get(symbol)
        return None

    # -- subscription plan ---------------------------------------------------
    @property
    def plan(self) -> SubscriptionPlan:
        return self._plan

    def current_plan(self) -> SubscriptionPlan:
        return self._plan

    def symbols(self) -> list[str]:
        return sorted({*self._plan.all, *self._extra})

    def set_plan_from_demand(
        self, demand: Iterable[SymbolDemand], universe: Iterable[str]
    ) -> SubscriptionPlan:
        if self.simulated is not None:  # no vendor cap for the simulator: serve the whole universe
            merged = [*demand, *(SymbolDemand(s) for s in universe)]
            self._plan = plan_subscriptions(merged, cap=10**6, warn_at=10**6)
        else:
            self._plan = plan_subscriptions(
                demand, cap=self.cfg.ws_symbol_cap, warn_at=self.cfg.ws_warn_at, fallback_universe=universe
            )
        return self._plan

    async def refresh_plan(self) -> SubscriptionPlan:
        try:
            async with get_db().pool.acquire() as conn:
                demand = await load_demand(conn)
                universe = await load_universe(conn)
        except (asyncpg.PostgresError, OSError, RuntimeError, TimeoutError) as exc:
            log.warning("subscription refresh failed", extra={"error": type(exc).__name__})
            return self._plan
        return self.set_plan_from_demand(demand, universe)

    async def load_anchors(self) -> None:
        """Last known closes from `prices_daily` -> simulator anchors and `prev_close` of every quote."""
        if self.simulated is None and self.supervisor is None:
            return
        today = self.calendar.exchange_date(self.clock.now())
        try:
            async with get_db().pool.acquire() as conn:
                rows = await conn.fetch(
                    "select distinct on (symbol) symbol, close from prices_daily where date < $1 "
                    "order by symbol, date desc",
                    today,
                )
        except (asyncpg.PostgresError, OSError, RuntimeError, TimeoutError) as exc:
            log.warning("anchor load failed", extra={"error": type(exc).__name__})
            return
        for r in rows:
            close = Decimal(r["close"])
            self.store.set_prev_close(r["symbol"], close)
            if self.simulated is not None:
                self.simulated.set_anchor(r["symbol"], close)

    def seed_store(self) -> None:
        """Simulator only: give every subscribed symbol a first quote so /quotes works before the first tick."""
        if self.simulated is None:
            return
        now = self.clock.now()
        for symbol in self.symbols():
            q = self.simulated.current(symbol)
            self.store.put(
                StoredQuote(symbol, q.price, now, now, SOURCE_LABEL, "stream", q.bid, q.ask, q.prev_close)
            )

    # -- simulator loop --------------------------------------------------------
    async def tick_once(self) -> int:
        """One simulator interval. Returns the number of quotes produced (0 when paused)."""
        sim = self.simulated
        if sim is None or self.session_now() != "regular":
            return 0
        feed = self.feed_override
        if feed in ("stale", "reconnecting"):
            return 0  # the feed is "down": quotes genuinely stop and age
        self._tick_n += 1
        if feed == "delayed" and self._tick_n % _DELAYED_TICK_EVERY:
            return 0
        now = self.clock.now()
        quotes = sim.step(self.symbols())
        for q in quotes:
            self.on_quote(
                StoredQuote(q.symbol, q.price, now, now, SOURCE_LABEL, "stream", q.bid, q.ask, q.prev_close)
            )
        return len(quotes)

    async def _simulator_loop(self) -> None:
        assert self.simulated is not None
        while True:
            await asyncio.sleep(self.simulated.tick_s)
            try:
                await self.tick_once()
            except Exception as exc:
                log.error("simulator tick failed", extra={"error": type(exc).__name__})

    async def _plan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.subscription_refresh_s)
            before = set(self.symbols())
            await self.refresh_plan()
            if self.simulated is not None and set(self.symbols()) - before:
                self.seed_store()

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        await self.refresh_plan()
        await self.load_anchors()
        self.seed_store()
        if not self.cfg.autostart:
            return
        self._tasks.append(asyncio.create_task(self._plan_loop(), name="marketdata-plan"))
        if self.simulated is not None:
            self._tasks.append(asyncio.create_task(self._simulator_loop(), name="marketdata-sim"))
        elif self.supervisor is not None:
            if self.supervisor.stream is not None:
                self._tasks.append(asyncio.create_task(self.supervisor.run_ws(), name="marketdata-ws"))
            self._tasks.append(asyncio.create_task(self.supervisor.run_poll(), name="marketdata-poll"))

    async def stop(self) -> None:
        if self.supervisor is not None:
            self.supervisor.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
        for close in self.closers:
            try:
                await close()
            except Exception as exc:
                log.debug("closer failed", extra={"error": type(exc).__name__})
        self.closers.clear()
