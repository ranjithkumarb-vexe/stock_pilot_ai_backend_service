"""JWT verification (Supabase HS256 / RS256 / ES256 via JWKS, or dev tokens) and stream tokens.

Same claim shape in both modes: `sub` (UUID user id), `email`, `aud=authenticated`. Tokens are never
logged. FastAPI wiring (`CurrentUser`) lives in `app.core.deps`.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt
from jwt import PyJWK

from app.core.config import Settings
from app.core.db import Connection, get_db
from app.core.errors import unauthenticated
from app.core.logging import get_logger

log = get_logger("app.auth")

DEV_ISSUER = "stockpilot-dev"
_JWKS_ALGS = ["RS256", "ES256"]
_JWKS_REFETCH_COOLDOWN_S = 30.0


@dataclass(frozen=True, slots=True)
class AuthUser:
    user_id: UUID
    email: str | None


def issue_dev_token(
    settings: Settings,
    user_id: UUID,
    email: str | None,
    *,
    now: datetime,
    ttl_s: int | None = None,
) -> str:
    """Sign a dev token (AUTH_MODE=dev). `ttl_s` may be negative in tests to mint expired tokens."""
    ttl = settings.dev_token_ttl_s if ttl_s is None else ttl_s
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "aud": settings.jwt_audience,
        "iss": DEV_ISSUER,
        "role": "authenticated",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    if email:
        claims["email"] = email
    return jwt.encode(claims, settings.dev_jwt_secret, algorithm="HS256")


class TokenVerifier:
    """Verifies bearer tokens according to `AUTH_MODE`. Algorithm allow-lists are fixed per key type
    (prevents alg-confusion and `none`)."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self._http = http
        self._owns_http = http is None
        self._jwks: dict[str, PyJWK] = {}
        self._jwks_fetched_at = 0.0
        self._jwks_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()

    async def verify(self, token: str) -> AuthUser:
        try:
            claims = await self._decode(token)
        except jwt.ExpiredSignatureError:
            raise unauthenticated("Token expired") from None
        except (jwt.InvalidTokenError, KeyError, ValueError):
            raise unauthenticated("Invalid token") from None
        except (httpx.HTTPError, TimeoutError):
            log.error("jwks fetch failed")
            raise unauthenticated("Unable to verify token") from None
        try:
            user_id = UUID(str(claims["sub"]))
        except (KeyError, ValueError):
            raise unauthenticated("Invalid token subject") from None
        email = claims.get("email")
        return AuthUser(user_id=user_id, email=email if isinstance(email, str) and email else None)

    # ------------------------------------------------------------------
    async def _decode(self, token: str) -> dict[str, Any]:
        s = self.s
        options: dict[str, Any] = {"require": ["exp", "sub"]}
        common: dict[str, Any] = {
            "audience": s.jwt_audience,
            "leeway": s.jwt_leeway_s,
            "options": options,
        }
        if s.jwt_issuer:
            common["issuer"] = s.jwt_issuer
        if s.auth_mode == "dev":
            return jwt.decode(token, s.dev_jwt_secret, algorithms=["HS256"], **common)

        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg == "HS256":
            if not s.supabase_jwt_secret:
                raise jwt.InvalidAlgorithmError("HS256 not configured")
            return jwt.decode(token, s.supabase_jwt_secret, algorithms=["HS256"], **common)
        if alg in _JWKS_ALGS:
            if not s.supabase_jwks_url:
                raise jwt.InvalidAlgorithmError("JWKS not configured")
            key = await self._jwks_key(header.get("kid"))
            return jwt.decode(token, key.key, algorithms=[str(alg)], **common)
        raise jwt.InvalidAlgorithmError("unsupported algorithm")

    async def _jwks_key(self, kid: str | None) -> PyJWK:
        if not isinstance(kid, str) or not kid:
            raise jwt.InvalidTokenError("missing kid")
        now = time.monotonic()
        fresh = now - self._jwks_fetched_at < self.s.jwks_cache_ttl_s
        if fresh and kid in self._jwks:
            return self._jwks[kid]
        async with self._jwks_lock:
            now = time.monotonic()
            fresh = now - self._jwks_fetched_at < self.s.jwks_cache_ttl_s
            if fresh and kid in self._jwks:
                return self._jwks[kid]
            # unknown kid: refetch, but not more often than the cooldown (JWKS-flood protection)
            cooled = now - self._jwks_fetched_at >= _JWKS_REFETCH_COOLDOWN_S
            if not self._jwks or not fresh or cooled:
                await self._refresh_jwks()
            try:
                return self._jwks[kid]
            except KeyError:
                raise jwt.InvalidTokenError("unknown kid") from None

    async def _refresh_jwks(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        resp = await self._http.get(self.s.supabase_jwks_url)
        resp.raise_for_status()
        keys: dict[str, PyJWK] = {}
        for jwk in resp.json().get("keys", []):
            try:
                if isinstance(jwk.get("kid"), str):
                    keys[jwk["kid"]] = PyJWK(jwk)
            except jwt.PyJWTError:
                continue
        self._jwks = keys
        self._jwks_fetched_at = time.monotonic()


# --------------------------------------------------------------------------
# Stream tokens: single-use, short-lived, stored hashed (SSE cannot send Authorization headers)
# --------------------------------------------------------------------------
def hash_stream_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_stream_token(conn: Connection, user_id: UUID, now: datetime, ttl_s: int) -> str:
    """Insert a new stream token for `user_id` (call inside `user_tx`); returns the clear token once."""
    token = secrets.token_urlsafe(32)
    await conn.execute("delete from stream_tokens where user_id = $1 and expires_at < $2", user_id, now)
    await conn.execute(
        "insert into stream_tokens (token_hash, user_id, created_at, expires_at) values ($1, $2, $3, $4)",
        hash_stream_token(token),
        user_id,
        now,
        now + timedelta(seconds=ttl_s),
    )
    return token


async def consume_stream_token(token: str, now: datetime) -> UUID | None:
    """Validate and burn a stream token (atomic, single use). Returns the user id or None.

    Runs through the SECURITY DEFINER function `consume_stream_token(hash, now)` because the SSE request
    has no user context yet. Stream C calls this at the top of `GET /v1/stream`.
    """
    if not token or len(token) > 128:
        return None
    async with get_db().pool.acquire() as conn:
        value = await conn.fetchval("select consume_stream_token($1, $2)", hash_stream_token(token), now)
    return value if isinstance(value, UUID) else None
