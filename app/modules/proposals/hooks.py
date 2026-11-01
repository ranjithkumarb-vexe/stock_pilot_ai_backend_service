"""Hooks other streams attach to the proposal workflow. Defaults are no-ops (nothing registered).

Stream C's bandit: `register_decision_hook(my_fn)` where
`async def my_fn(conn, user_id, proposal, reward) -> None`. It runs INSIDE the decision transaction (so the
reward and the decision commit together), `reward` is 1.0 approve / 0.5 modify / 0.0 reject (SPEC 10.3C), and
`proposal` is the serialised proposal dict (`id`, `kind`, `symbol`, `rule`, `status`, ...). A failing hook is
logged and rolled back on its own (savepoint); it never fails the decision.
`on_decision` is the dispatcher the workflow calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.core.db import Connection
from app.core.logging import get_logger

log = get_logger("app.proposals.hooks")

DecisionHook = Callable[[Connection, UUID, dict[str, Any], float], Awaitable[None]]
CreatedHook = Callable[[Connection, UUID, dict[str, Any]], Awaitable[None]]

_DECISION_HOOKS: list[DecisionHook] = []
_CREATED_HOOKS: list[CreatedHook] = []

REWARD_APPROVE = 1.0
REWARD_MODIFY = 0.5
REWARD_REJECT = 0.0


def register_decision_hook(hook: DecisionHook) -> None:
    if hook not in _DECISION_HOOKS:
        _DECISION_HOOKS.append(hook)


def register_created_hook(hook: CreatedHook) -> None:
    """e.g. SSE push of a new proposal."""
    if hook not in _CREATED_HOOKS:
        _CREATED_HOOKS.append(hook)


def clear_hooks() -> None:  # tests
    _DECISION_HOOKS.clear()
    _CREATED_HOOKS.clear()


async def _run(conn: Connection, name: str, fn: Callable[..., Awaitable[None]], *args: Any) -> None:
    try:
        async with conn.transaction():
            await fn(conn, *args)
    except Exception as exc:
        log.error(
            "proposal hook failed", extra={"hook": getattr(fn, "__name__", name), "error": type(exc).__name__}
        )


async def on_decision(conn: Connection, user_id: UUID, proposal: dict[str, Any], reward: float) -> None:
    for hook in list(_DECISION_HOOKS):
        await _run(conn, "decision", hook, user_id, proposal, reward)


async def on_created(conn: Connection, user_id: UUID, proposal: dict[str, Any]) -> None:
    for hook in list(_CREATED_HOOKS):
        await _run(conn, "created", hook, user_id, proposal)
