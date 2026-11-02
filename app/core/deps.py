"""FastAPI dependencies shared by all modules.

    from app.core.deps import CurrentUser, UserConn

    @router.get("/thing")
    async def thing(user: CurrentUser, conn: UserConn): ...

`UserConn` is a connection inside a transaction with `app.user_id` set (RLS enforced). The transaction
commits *before* the response is sent (`scope="function"`), so a client never reads stale state.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request

from app.core.auth import AuthUser, TokenVerifier
from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import Connection, user_tx
from app.core.errors import AppError, ErrorCode, unauthenticated
from app.core.logging import user_id_var
from app.core.ratelimit import TokenBucketLimiter
from app.core.users import KnownUsers


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


async def get_current_user(request: Request) -> AuthUser:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthenticated("Missing bearer token")
    verifier: TokenVerifier = request.app.state.token_verifier
    user = await verifier.verify(token.strip())

    limiter: TokenBucketLimiter | None = request.app.state.user_limiter
    if limiter is not None:
        wait = limiter.check(f"user:{user.user_id}")
        if wait > 0:
            retry = max(1, math.ceil(wait))
            raise AppError(
                ErrorCode.RATE_LIMITED,
                "Too many requests",
                details={"retry_after_s": retry},
                headers={"Retry-After": str(retry)},
            )
    user_id_var.set(str(user.user_id))
    known: KnownUsers = request.app.state.known_users
    await known.ensure(user)
    return user


async def expensive_rate_limit(
    request: Request, user: Annotated[AuthUser, Depends(get_current_user)]
) -> None:
    """Extra per-user bucket for endpoints that can spend LLM budget (SEC-17). Add with `dependencies=[ExpensiveCall]`."""
    limiter: TokenBucketLimiter | None = request.app.state.expensive_limiter
    if limiter is None:
        return
    wait = limiter.check(f"expensive:{user.user_id}")
    if wait > 0:
        retry = max(1, math.ceil(wait))
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "Too many requests for this action, slow down",
            details={"retry_after_s": retry},
            headers={"Retry-After": str(retry)},
        )


async def user_conn(
    user: Annotated[AuthUser, Depends(get_current_user)],
) -> AsyncIterator[Connection]:
    async with user_tx(user.user_id) as conn:
        yield conn


CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
ExpensiveCall = Depends(expensive_rate_limit)
UserConn = Annotated[Connection, Depends(user_conn, scope="function")]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
ClockDep = Annotated[Clock, Depends(get_clock)]

__all__ = [
    "ClockDep",
    "CurrentUser",
    "ExpensiveCall",
    "SettingsDep",
    "UserConn",
    "get_clock",
    "get_current_user",
]
