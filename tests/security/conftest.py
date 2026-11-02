"""Fixtures for the SEC-01 security regression suite (see docs/security/FINDINGS.md).

Everything here goes through the real ASGI app and the real (RLS-enforcing) database role. Tests of findings that
are still open are marked ``xfail(strict=True, reason="SEC-xx ...")``: they flip to a failure (XPASS) the moment
the fix lands, which is the signal to delete the marker and set the finding to ``fixed``.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import jwt
import pytest
import pytest_asyncio

from app.core.config import Settings
from app.core.db import vec_literal
from tests.db.rls_specs import Prereq, create_prereq

Conn = Any
MakeUser = Callable[..., Awaitable[UUID]]
AuthHeaders = Callable[..., dict[str, str]]

# markers planted in tenant A's data; they must never show up in any response served to tenant B
MARK_CHAT = "ZX-SECRET-CHAT-7731"
MARK_MEMORY = "ZX-SECRET-MEMORY-5519"
MARK_EMAIL_LOCAL = "tenant-a-private"


@dataclass
class Tenant:
    uid: UUID
    headers: dict[str, str]
    email: str
    prereq: Prereq | None = None
    alert_id: UUID | None = None
    memory_id: UUID | None = None
    citation_id: UUID | None = None
    message_id: UUID | None = None

    @property
    def p(self) -> Prereq:
        assert self.prereq is not None
        return self.prereq


@pytest_asyncio.fixture
async def tenant_a(db_admin: Conn, ensure_tickers: Any, auth_headers: AuthHeaders) -> AsyncIterator[Tenant]:
    """User A with one row in every id-addressable resource, all carrying the leak markers."""
    await ensure_tickers(["AAPL"])
    p = await create_prereq(db_admin)
    email = f"{MARK_EMAIL_LOCAL}-{uuid4().hex[:6]}@example.test"
    await db_admin.execute(
        "update users set email = $2, display_name = 'Tenant A' where id = $1", p.user_id, email
    )
    await db_admin.execute("update chat_threads set title = $2 where id = $1", p.thread_id, MARK_CHAT)
    alert = await db_admin.fetchval(
        "insert into alerts (user_id, symbol, type, dedupe_key) values ($1, 'AAPL', 'filing', $2) returning id",
        p.user_id,
        uuid4().hex,
    )
    mem = await db_admin.fetchval(
        "insert into memories (user_id, kind, text, embedding) values ($1, 'semantic', $2, $3::vector) returning id",
        p.user_id,
        MARK_MEMORY,
        vec_literal([0.01] * 1024),
    )
    cit = await db_admin.fetchval(
        "insert into citations (brief_id, user_id, kind, ref_id) values ($1, $2, 'xbrl', '1') returning id",
        p.brief_id,
        p.user_id,
    )
    msg = await db_admin.fetchval(
        "insert into chat_messages (thread_id, user_id, role, content) values ($1, $2, 'user', $3) returning id",
        p.thread_id,
        p.user_id,
        MARK_CHAT,
    )
    try:
        yield Tenant(p.user_id, auth_headers(p.user_id, email), email, p, alert, mem, cit, msg)
    finally:
        await db_admin.execute("delete from users where id = $1", p.user_id)


@pytest_asyncio.fixture
async def tenant_b(make_user: MakeUser, auth_headers: AuthHeaders) -> Tenant:
    uid = await make_user(email=f"tenant-b-{uuid4().hex[:6]}@example.test", display_name="Tenant B")
    return Tenant(uid, auth_headers(uid), "b")


@pytest_asyncio.fixture
async def api(app_factory: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    """Real app, rate limiting off (the rate-limit tests build their own)."""
    async with app_factory(rate_limit_enabled=False) as client:
        yield client


# ---------------------------------------------------------------------------------------------------------------
# token forging helpers
# ---------------------------------------------------------------------------------------------------------------
def claims_for(
    uid: UUID | str, *, aud: str | None = "authenticated", ttl_s: int = 600, **over: Any
) -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    c: dict[str, Any] = {
        "sub": str(uid),
        "aud": aud,
        "iat": now,
        "exp": now + ttl_s,
        "email": "x@example.test",
    }
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def forged_tokens(settings: Settings, uid: UUID) -> dict[str, str]:
    """Tokens a dev-mode server must reject (every one is structurally a JWT)."""
    good = settings.dev_jwt_secret
    other = "attacker-secret-attacker-secret-0123456789"
    none_tok = jwt.encode(claims_for(uid), None, algorithm="none")  # type: ignore[arg-type]
    return {
        "wrong-secret": jwt.encode(claims_for(uid), other, algorithm="HS256"),
        "alg-none": none_tok,
        "alg-none-uppercase-header": re.sub(
            r"^[^.]+", "eyJhbGciOiJOT05FIiwidHlwIjoiSldUIn0", none_tok, count=1
        ),
        "expired": jwt.encode(claims_for(uid, ttl_s=-3600), good, algorithm="HS256"),
        "wrong-audience": jwt.encode(claims_for(uid, aud="service_role"), good, algorithm="HS256"),
        "no-audience": jwt.encode(claims_for(uid, aud=None), good, algorithm="HS256"),
        "no-exp": jwt.encode(claims_for(uid, exp=None), good, algorithm="HS256"),
        "no-sub": jwt.encode(claims_for(uid, sub=None), good, algorithm="HS256"),
        "sub-not-uuid": jwt.encode(claims_for(uid, sub="1 OR 1=1"), good, algorithm="HS256"),
        "sub-sql": jwt.encode(
            claims_for(uid, sub="00000000-0000-0000-0000-000000000000' OR '1'='1"), good, algorithm="HS256"
        ),
        "hs512-with-right-secret": jwt.encode(claims_for(uid), good, algorithm="HS512"),
        "truncated": jwt.encode(claims_for(uid), good, algorithm="HS256")[:-6],
        "garbage": "not.a.jwt",
        "empty-parts": "..",
    }


@pytest.fixture
def forge(settings: Settings) -> Callable[[UUID], dict[str, str]]:
    return lambda uid: forged_tokens(settings, uid)


# ---------------------------------------------------------------------------------------------------------------
# route table from the live OpenAPI document
# ---------------------------------------------------------------------------------------------------------------
_PARAM_SAMPLES: dict[str, str] = {"symbol": "AAPL", "output_type": "alert"}


def concrete_path(template: str) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in _PARAM_SAMPLES:
            return _PARAM_SAMPLES[name]
        if name.endswith("_id") or name == "trace_ref":
            return str(uuid4())
        return "x"

    return re.sub(r"\{([^}]+)\}", sub, template)


def route_table(app: Any) -> list[tuple[str, str]]:
    """[(METHOD, template)] for every operation in the app's OpenAPI schema."""
    spec = app.openapi()
    out: list[tuple[str, str]] = []
    for path, ops in sorted(spec["paths"].items()):
        for method in ops:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                out.append((method.upper(), path))
    return out


@pytest.fixture
def dev_login_headers(api: httpx.AsyncClient) -> Callable[[str], Awaitable[dict[str, str]]]:
    async def _login(persona: str = "new") -> dict[str, str]:
        r = await api.post("/v1/auth/dev-login", json={"persona": persona})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _login


__all__ = ["MARK_CHAT", "MARK_EMAIL_LOCAL", "MARK_MEMORY", "Tenant"]


# ---------------------------------------------------------------------------------------------------------------
# a minimal paper-trading environment (no dependency on scripts/seed*.py, which other agents edit)
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Trader:
    client: httpx.AsyncClient
    headers: dict[str, str]
    uid: UUID


@pytest_asyncio.fixture
async def trader(app_factory: Callable[..., Any], db_admin: Conn) -> AsyncIterator[Trader]:
    """An onboarded user with $100k paper cash, AAPL at 100.00, market forced open, fake clock = now."""
    from app.core.clock import FakeClock
    from app.core.db import get_db
    from app.modules.paper.pricing import DbPriceSource

    # Track exactly what this fixture inserts so teardown can remove it: leftover AAPL rows (price 100.00 etc.)
    # would otherwise corrupt every later test that seeds or reads AAPL in the shared test database (CR-REL-5).
    made_ticker = await db_admin.fetchval(
        "insert into tickers (symbol, name, sector, in_universe) values ('AAPL','Apple','Technology',true) "
        "on conflict do nothing returning symbol"
    )
    price_dates = [
        r["date"]
        for r in await db_admin.fetch(
            "insert into prices_daily (symbol, date, open, high, low, close, volume) "
            "values ('AAPL', current_date - 1, 100, 101, 99, 100, 1000), "
            "('AAPL', current_date - 2, 99, 101, 98, 99, 1000) on conflict do nothing returning date"
        )
    ]
    clock = FakeClock(start=datetime.now(UTC))
    async with app_factory(clock=clock, rate_limit_enabled=False) as client:
        app = client.app
        app.state.market_override = True
        app.state.price_source = DbPriceSource(get_db().pool, app.state, clock.now)
        r = await client.post("/v1/auth/dev-login", json={"persona": "new"})
        assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        uid = UUID(r.json()["user"]["user_id"])
        body = {
            "answers": [4, 3, 3, 3, 3, 3],
            "horizon": "3-10 years",
            "goals": ["Growth"],
            "experience": "Intermediate",
            "watchlist": ["AAPL"],
            "starting_cash": 100000,
            "disclaimer_ack": True,
        }
        ob = await client.post("/v1/onboarding", headers=headers, json=body)
        assert ob.status_code in (200, 201), ob.text
        try:
            yield Trader(client, headers, uid)
        finally:
            await db_admin.execute("delete from users where id = $1", uid)
            if price_dates:
                await db_admin.execute(
                    "delete from prices_daily where symbol = 'AAPL' and date = any($1::date[])", price_dates
                )
            if made_ticker:
                with contextlib.suppress(asyncpg.PostgresError):  # still referenced elsewhere: leave it
                    await db_admin.execute("delete from tickers where symbol = 'AAPL'")
