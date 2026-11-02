"""Application factory.

Modules are auto-discovered: any package `app/modules/<name>/` containing `router.py` that exposes
`router: APIRouter` is mounted under `/v1` (override with a module-level `PREFIX = ""` or another prefix).
A router module may also expose `lifespan(app) -> AsyncContextManager[None]` for startup/shutdown work
(schedulers, quote stores). Nobody edits this file to add a module.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.modules as modules_pkg
from app.core.auth import TokenVerifier
from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.db import Database, set_database
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    BodyLimitMiddleware,
    ErrorBoundaryMiddleware,
    SecurityHeadersMiddleware,
    TraceMiddleware,
)
from app.core.ratelimit import IpRateLimitMiddleware, TokenBucketLimiter
from app.core.users import KnownUsers

log = get_logger("app.main")

DEFAULT_PREFIX = "/v1"


def discover_modules() -> list[tuple[str, Any]]:
    """Import `app.modules.<name>.router` for every module package that has one."""
    found: list[tuple[str, Any]] = []
    for info in sorted(pkgutil.iter_modules(modules_pkg.__path__), key=lambda i: i.name):
        if not info.ispkg:
            continue
        target = f"{modules_pkg.__name__}.{info.name}.router"
        try:
            mod = importlib.import_module(target)
        except ModuleNotFoundError as exc:
            if exc.name == target:  # package without a router.py: not an API module
                continue
            raise
        if not isinstance(getattr(mod, "router", None), APIRouter):
            raise RuntimeError(f"{target} must expose `router: APIRouter`")
        found.append((info.name, mod))
    return found


def create_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings)
        await db.open()
        set_database(db)
        app.state.db = db
        async with AsyncExitStack() as stack:
            for name, mod in app.state.module_list:
                hook = getattr(mod, "lifespan", None)
                if hook is not None:
                    await stack.enter_async_context(hook(app))
                    log.info("module lifespan started", extra={"api_module": name})
            log.info("startup complete", extra={"db_degraded": db.degraded, "env": settings.app_env})
            try:
                yield
            finally:
                log.info("shutting down")
        await app.state.token_verifier.aclose()
        set_database(None)
        await db.close()

    app = FastAPI(
        title="StockPilot AI API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    app.state.token_verifier = TokenVerifier(settings)
    app.state.known_users = KnownUsers()
    monotonic = app.state.clock.monotonic
    app.state.user_limiter = (
        TokenBucketLimiter(settings.rate_limit_user_burst, settings.rate_limit_user_per_s, monotonic)
        if settings.rate_limit_enabled
        else None
    )

    app.state.expensive_limiter = (
        TokenBucketLimiter(
            settings.rate_limit_expensive_burst, settings.rate_limit_expensive_per_s, monotonic
        )
        if settings.rate_limit_enabled
        else None
    )

    register_exception_handlers(app)

    # add_middleware: last added is outermost
    if settings.rate_limit_enabled:
        ip_limiter = TokenBucketLimiter(settings.rate_limit_ip_burst, settings.rate_limit_ip_per_s, monotonic)
        app.add_middleware(
            IpRateLimitMiddleware,
            limiter=ip_limiter,
            trust_proxy=settings.trust_proxy,
            trusted_hops=settings.trusted_proxy_hops,
        )
        app.state.ip_limiter = ip_limiter
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(ErrorBoundaryMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Trace-Id",
                "Last-Event-ID",
            ],
            expose_headers=["X-Trace-Id", "Retry-After", "Idempotent-Replay"],
            max_age=600,
        )
    app.add_middleware(TraceMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    module_list = discover_modules()
    app.state.module_list = module_list
    for name, mod in module_list:
        prefix = getattr(mod, "PREFIX", DEFAULT_PREFIX)
        app.include_router(mod.router, prefix=prefix)
        log.info("module mounted", extra={"api_module": name, "prefix": prefix})
    return app
