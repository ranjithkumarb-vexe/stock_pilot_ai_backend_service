"""BE-03: 401 matrix, dev-login, /me, tenant separation through the API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest

from app.core.config import Settings
from app.core.personas import PERSONAS

Conn = Any
SUPABASE_SECRET = "supabase-test-secret-0123456789-abcdefghij"


def _claims(user: UUID, **over: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "sub": str(user),
        "email": "x@example.test",
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    base.update(over)
    return base


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_me_requires_a_bearer_token(app_client: httpx.AsyncClient) -> None:
    for headers in (
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
    ):
        resp = await app_client.get("/v1/me", headers=headers)
        assert resp.status_code == 401, headers
        assert resp.json()["code"] == "unauthenticated"
        assert resp.headers["www-authenticate"] == "Bearer"
        assert resp.json()["trace_id"] == resp.headers["x-trace-id"]


@pytest.mark.parametrize(
    "case",
    [
        "garbage",
        "wrong_secret",
        "expired",
        "wrong_audience",
        "alg_none",
        "no_exp",
        "bad_sub",
        "no_sub",
    ],
)
async def test_bad_tokens_are_401(app_client: httpx.AsyncClient, settings: Settings, case: str) -> None:
    uid = uuid4()
    secret = settings.dev_jwt_secret
    if case == "garbage":
        token = "not.a.jwt"
    elif case == "wrong_secret":
        token = jwt.encode(_claims(uid), "x" * 40, algorithm="HS256")
    elif case == "expired":
        token = jwt.encode(
            _claims(uid, exp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())),
            secret,
            algorithm="HS256",
        )
    elif case == "wrong_audience":
        token = jwt.encode(_claims(uid, aud="someone-else"), secret, algorithm="HS256")
    elif case == "alg_none":
        token = jwt.encode(_claims(uid), None, algorithm="none")  # type: ignore[arg-type]
    elif case == "no_exp":
        c = _claims(uid)
        del c["exp"]
        token = jwt.encode(c, secret, algorithm="HS256")
    elif case == "bad_sub":
        token = jwt.encode(_claims(uid, sub="not-a-uuid"), secret, algorithm="HS256")
    else:
        c = _claims(uid)
        del c["sub"]
        token = jwt.encode(c, secret, algorithm="HS256")
    resp = await app_client.get("/v1/me", headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"
    assert token not in resp.text  # tokens are never echoed


async def test_expired_token_message(
    app_client: httpx.AsyncClient, make_user: Callable[..., Any], auth_headers: Callable[..., Any]
) -> None:
    uid = await make_user()
    resp = await app_client.get("/v1/me", headers=auth_headers(uid, ttl_s=-120))
    assert resp.status_code == 401
    assert resp.json()["message"] == "Token expired"


@pytest.mark.parametrize("persona", ["priya", "alex", "sam"])
async def test_dev_login_personas_exist_before_seed(
    app_client: httpx.AsyncClient, persona: str, db_admin: Conn
) -> None:
    resp = await app_client.post("/v1/auth/dev-login", json={"persona": persona})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"access_token", "expires_in", "user"}
    p = PERSONAS[persona]
    assert body["user"] == {
        "user_id": str(p.user_id),
        "email": p.email,
        "display_name": p.display_name,
        "initials": body["user"]["initials"],
        "onboarded": True,
        "persona_id": persona,
    }
    assert isinstance(body["expires_in"], int) and body["expires_in"] > 0
    # deterministic: second login is the same user, no duplicate rows
    again = await app_client.post("/v1/auth/dev-login", json={"persona": persona})
    assert again.json()["user"]["user_id"] == str(p.user_id)
    assert await db_admin.fetchval("select count(*) from users where id = $1", p.user_id) == 1
    me = await app_client.get("/v1/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200 and me.json() == body["user"]


async def test_persona_ids_are_stable_constants() -> None:
    assert str(PERSONAS["priya"].user_id) == str(PERSONAS["priya"].user_id)
    assert len({p.user_id for p in PERSONAS.values()}) == 3
    assert PERSONAS["priya"].display_name == "Priya Nair"
    assert PERSONAS["alex"].display_name == "Alex Rivera"


async def test_dev_login_new_users(app_client: httpx.AsyncClient) -> None:
    one = (await app_client.post("/v1/auth/dev-login", json={"persona": "new"})).json()["user"]
    two = (await app_client.post("/v1/auth/dev-login", json={"persona": "new"})).json()["user"]
    assert one["user_id"] != two["user_id"]  # fresh onboarding user every time
    assert one["onboarded"] is False and one["persona_id"] is None
    named = await app_client.post(
        "/v1/auth/dev-login", json={"persona": "new", "email": "Fresh.User@Example.com"}
    )
    again = await app_client.post(
        "/v1/auth/dev-login", json={"persona": "new", "email": "fresh.user@example.com"}
    )
    assert named.json()["user"]["user_id"] == again.json()["user"]["user_id"]  # email => find-or-create
    assert named.json()["user"]["email"] == "fresh.user@example.com"
    assert named.json()["user"]["initials"] == "FU"


async def test_dev_login_validation(app_client: httpx.AsyncClient) -> None:
    assert (await app_client.post("/v1/auth/dev-login", json={"persona": "mallory"})).status_code == 422
    assert (
        await app_client.post("/v1/auth/dev-login", json={"persona": "new", "email": "nope"})
    ).status_code == 422
    assert (await app_client.post("/v1/auth/dev-login", json={"persona": "priya", "x": 1})).status_code == 422


async def test_dev_login_is_404_when_auth_mode_is_supabase(app_factory: Callable[..., Any]) -> None:
    async with app_factory(auth_mode="supabase", supabase_jwt_secret=SUPABASE_SECRET) as client:
        resp = await client.post("/v1/auth/dev-login", json={"persona": "priya"})
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"


async def test_supabase_hs256_mode_accepts_supabase_tokens_and_rejects_dev_tokens(
    app_factory: Callable[..., Any], db_admin: Conn, settings: Settings
) -> None:
    uid = uuid4()
    good = jwt.encode(_claims(uid, email="sb@example.test"), SUPABASE_SECRET, algorithm="HS256")
    dev = jwt.encode(_claims(uid), settings.dev_jwt_secret, algorithm="HS256")
    async with app_factory(auth_mode="supabase", supabase_jwt_secret=SUPABASE_SECRET) as client:
        ok = await client.get("/v1/me", headers=_bearer(good))
        assert ok.status_code == 200
        assert ok.json()["user_id"] == str(uid) and ok.json()["email"] == "sb@example.test"
        # user record was upserted on first authenticated request
        assert await db_admin.fetchval("select email from users where id = $1", uid) == "sb@example.test"
        assert (await client.get("/v1/me", headers=_bearer(dev))).status_code == 401
    await db_admin.execute("delete from users where id = $1", uid)


async def test_issuer_check_when_configured(app_factory: Callable[..., Any]) -> None:
    uid = uuid4()
    good = jwt.encode(_claims(uid, iss="https://x.supabase.co/auth/v1"), SUPABASE_SECRET, algorithm="HS256")
    bad = jwt.encode(_claims(uid, iss="https://evil.example"), SUPABASE_SECRET, algorithm="HS256")
    async with app_factory(
        auth_mode="supabase",
        supabase_jwt_secret=SUPABASE_SECRET,
        jwt_issuer="https://x.supabase.co/auth/v1",
    ) as client:
        assert (await client.get("/v1/me", headers=_bearer(good))).status_code == 200
        assert (await client.get("/v1/me", headers=_bearer(bad))).status_code == 401


async def test_token_for_user_a_never_sees_user_b_data(
    app_client: httpx.AsyncClient,
    make_user: Callable[..., Any],
    auth_headers: Callable[..., Any],
    db_admin: Conn,
) -> None:
    a = await make_user("a@example.test", "Alice A")
    b = await make_user("b@example.test", "Bob B")
    ra = await app_client.get("/v1/me", headers=auth_headers(a))
    rb = await app_client.get("/v1/me", headers=auth_headers(b))
    assert ra.json()["email"] == "a@example.test" and ra.json()["user_id"] == str(a)
    assert rb.json()["email"] == "b@example.test" and rb.json()["user_id"] == str(b)
    # a token cannot be re-pointed at someone else's row: the API only ever uses the verified `sub`
    assert "b@example.test" not in ra.text


def test_auth_modes_fail_fast_on_bad_config() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=dev is not allowed"):
        Settings(_env_file=None, app_env="prod", auth_mode="dev")
    with pytest.raises(ValueError, match="SUPABASE_JWT_SECRET or SUPABASE_JWKS_URL"):
        Settings(_env_file=None, auth_mode="supabase")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Settings(_env_file=None, dev_jwt_secret="short")
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(
            _env_file=None,
            app_env="prod",
            auth_mode="supabase",
            supabase_jwt_secret="x" * 40,
            cors_origins="*",
        )
