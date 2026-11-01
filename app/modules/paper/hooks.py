"""Extension points other modules register on (kept dependency-free so `paper` never imports them).

`after_trade` hooks run after an order fills (or expires/cancels) inside the same transaction, wrapped in a
savepoint: a failing hook is logged and rolled back on its own and never fails the order.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.db import Connection
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.modules.paper.context import EngineContext

log = get_logger("app.paper.hooks")

AfterTradeHook = Callable[[Connection, UUID, "EngineContext"], Awaitable[None]]
_AFTER_TRADE: list[AfterTradeHook] = []


def register_after_trade(hook: AfterTradeHook) -> None:
    if hook not in _AFTER_TRADE:
        _AFTER_TRADE.append(hook)


def clear_after_trade_hooks() -> None:  # tests
    _AFTER_TRADE.clear()


async def run_after_trade(conn: Connection, user_id: UUID, ctx: EngineContext) -> None:
    for hook in list(_AFTER_TRADE):
        try:
            async with conn.transaction():
                await hook(conn, user_id, ctx)
        except Exception as exc:
            log.error(
                "after_trade hook failed",
                extra={"hook": getattr(hook, "__name__", "?"), "error": type(exc).__name__},
            )
