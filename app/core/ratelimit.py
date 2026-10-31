"""In-memory token-bucket rate limiting (single instance; Redis optional later, ADR-004)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import ErrorCode, error_response

_EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})
_MAX_BUCKETS = 50_000


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    """Keyed token buckets. `check` returns 0.0 when allowed, else seconds until a token is free."""

    def __init__(self, burst: int, per_second: float, now: Callable[[], float]) -> None:
        self.burst = float(burst)
        self.per_second = per_second
        self._now = now
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str, cost: float = 1.0) -> float:
        now = self._now()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= _MAX_BUCKETS:
                self._evict(now)
            bucket = self._buckets[key] = _Bucket(self.burst, now)
        else:
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.per_second)
            bucket.updated = now
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return 0.0
        return (cost - bucket.tokens) / self.per_second

    def _evict(self, now: float) -> None:
        """Drop buckets that have fully refilled (they are indistinguishable from new ones)."""
        full_after = self.burst / self.per_second
        stale = [k for k, b in self._buckets.items() if now - b.updated >= full_after]
        for k in stale:
            del self._buckets[k]
        if len(self._buckets) >= _MAX_BUCKETS:  # still full: drop oldest half
            for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1].updated)[: _MAX_BUCKETS // 2]:
                del self._buckets[k]


def rate_limited_error_response(retry_after_s: float) -> JSONResponse:
    retry = max(1, math.ceil(retry_after_s))
    return error_response(
        429,
        ErrorCode.RATE_LIMITED,
        "Too many requests",
        {"retry_after_s": retry},
        {"Retry-After": str(retry)},
    )


def client_ip(scope: Scope, trust_proxy: bool) -> str:
    if trust_proxy:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                first = str(value.decode("latin-1")).split(",")[0].strip()
                if first:
                    return first
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


class IpRateLimitMiddleware:
    """Per-IP bucket for every HTTP request except health probes. Per-user buckets are enforced in
    `app.core.deps.get_current_user` (after the token is verified, so a forged `sub` cannot drain
    someone else's budget)."""

    def __init__(self, app: ASGIApp, limiter: TokenBucketLimiter, trust_proxy: bool = False) -> None:
        self.app = app
        self.limiter = limiter
        self.trust_proxy = trust_proxy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _EXEMPT_PATHS or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        wait = self.limiter.check(f"ip:{client_ip(scope, self.trust_proxy)}")
        if wait > 0:
            await rate_limited_error_response(wait)(scope, receive, send)
            return
        await self.app(scope, receive, send)
