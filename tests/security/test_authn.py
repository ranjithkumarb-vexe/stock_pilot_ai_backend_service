"""SEC-01 area 1: authentication. Every route is enumerated from the live OpenAPI schema and probed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.auth import TokenVerifier
from app.core.config import DEV_JWT_SECRET_DEFAULT, Settings
from app.core.errors import AppError
from tests.prod_config import prod_kwargs
from tests.security.conftest import claims_for, concrete_path, route_table

pytestmark = pytest.mark.db

#: the only operations that may answer something other than 401 to a request with no credentials
PUBLIC_OPERATIONS = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/v1/universe"),
        ("GET", "/v1/onboarding/questions"),
        ("POST", "/v1/auth/dev-login"),  # 404 unless AUTH_MODE=dev (asserted separately)
    }
)
DEV_ONLY_OPERATIONS = frozenset(
    {
        ("POST", "/v1/auth/dev-login"),
        ("POST", "/v1/dev/feed"),
        ("POST", "/v1/dev/market"),
        ("POST", "/v1/dev/recompute-features"),
    }
)


def _body(method: str) -> dict[str, Any]:
    return {"json": {}} if method in {"POST", "PUT", "PATCH"} else {}


# ------------------------------------------------------------------------------------------ route enumeration
async def test_every_non_public_route_rejects_missing_credentials(api: httpx.AsyncClient) -> None:
    ops = route_table(api.app)  # type: ignore[attr-defined]
    assert len(ops) >= 45, "route discovery looks broken"
    leaks: list[str] = []
    for method, template in ops:
        if (method, template) in PUBLIC_OPERATIONS:
            continue
        r = await api.request(method, concrete_path(template), **_body(method))
        if r.status_code != 401:
            leaks.append(f"{method} {template} -> {r.status_code}")
        else:
            assert r.headers.get("www-authenticate") == "Bearer"
            assert r.json()["code"] == "unauthenticated"
    assert not leaks, f"routes reachable without credentials: {leaks}"


async def test_public_operations_are_exactly_the_documented_ones(api: httpx.AsyncClient) -> None:
    """A new unauthenticated route must be added to PUBLIC_OPERATIONS consciously (and reviewed)."""
    ops = {(m, t) for m, t in route_table(api.app)}  # type: ignore[attr-defined]
    assert ops >= PUBLIC_OPERATIONS
    unauth: set[tuple[str, str]] = set()
    for method, template in ops:
        r = await api.request(method, concrete_path(template), **_body(method))
        if r.status_code != 401:
            unauth.add((method, template))
    assert unauth == set(PUBLIC_OPERATIONS)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "bearer",
        "Token abc",
        "Bearer a b",
        "Bearer null",
        "Bearer undefined",
    ],
)
async def test_malformed_authorization_headers_are_401(api: httpx.AsyncClient, header: str) -> None:
    r = await api.get("/v1/me", headers={"Authorization": header} if header else {})
    assert r.status_code == 401


async def test_forged_tokens_rejected_on_every_route(
    api: httpx.AsyncClient, tenant_a: Any, forge: Callable[[UUID], dict[str, str]]
) -> None:
    """Every forgery class x every authenticated route (read + write). 401 and never data."""
    ops = [(m, t) for m, t in route_table(api.app) if (m, t) not in PUBLIC_OPERATIONS]  # type: ignore[attr-defined]
    failures: list[str] = []
    for name, token in forge(tenant_a.uid).items():
        for method, template in ops:
            r = await api.request(
                method, concrete_path(template), headers={"Authorization": f"Bearer {token}"}, **_body(method)
            )
            if r.status_code != 401:
                failures.append(f"{name}: {method} {template} -> {r.status_code}")
    assert not failures, failures[:10]


async def test_forged_token_error_does_not_reveal_why(
    api: httpx.AsyncClient, tenant_a: Any, forge: Callable[[UUID], dict[str, str]]
) -> None:
    bodies = set()
    for name, token in forge(tenant_a.uid).items():
        if name == "expired":
            continue
        r = await api.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
        bodies.add(r.json()["message"])
        assert "secret" not in r.text.lower() and "signature" not in r.text.lower()
    assert len(bodies) <= 2  # "Invalid token" / "Invalid token subject": no oracle on which check failed


async def test_expired_token_is_distinguishable_only_as_expired(
    api: httpx.AsyncClient, tenant_a: Any, forge: Callable[[UUID], dict[str, str]]
) -> None:
    r = await api.get("/v1/me", headers={"Authorization": f"Bearer {forge(tenant_a.uid)['expired']}"})
    assert r.status_code == 401 and r.json()["message"] == "Token expired"


async def test_valid_token_works_and_binds_identity_to_sub(
    api: httpx.AsyncClient, tenant_a: Any, tenant_b: Any
) -> None:
    ra = await api.get("/v1/me", headers=tenant_a.headers)
    rb = await api.get("/v1/me", headers=tenant_b.headers)
    assert ra.status_code == rb.status_code == 200
    assert ra.json()["user_id"] == str(tenant_a.uid) and rb.json()["user_id"] == str(tenant_b.uid)


async def test_client_supplied_identity_headers_and_params_are_ignored(
    api: httpx.AsyncClient, tenant_a: Any, tenant_b: Any
) -> None:
    """B cannot become A by sending X-User-Id / ?user_id= / body user_id."""
    h = {**tenant_b.headers, "X-User-Id": str(tenant_a.uid), "X-Forwarded-User": str(tenant_a.uid)}
    r = await api.get(f"/v1/me?user_id={tenant_a.uid}&sub={tenant_a.uid}", headers=h)
    assert r.json()["user_id"] == str(tenant_b.uid)
    r = await api.get(f"/v1/profile?user_id={tenant_a.uid}", headers=h)
    assert tenant_a.email not in r.text


# ------------------------------------------------------------------------------------------ dev surface vs prod
async def test_dev_surface_exists_only_in_dev_mode(app_factory: Callable[..., Any], tenant_a: Any) -> None:
    secret = "s" * 48
    async with app_factory(
        rate_limit_enabled=False, auth_mode="supabase", supabase_jwt_secret=secret, app_env="test"
    ) as client:
        token = jwt.encode(claims_for(tenant_a.uid), secret, algorithm="HS256")
        h = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/v1/me", headers=h)).status_code == 200
        bodies = {
            "/v1/auth/dev-login": {"persona": "priya"},
            "/v1/dev/market": {"open": True},
            "/v1/dev/feed": {"state": "live"},
            "/v1/dev/recompute-features": {},
        }
        for method, path in sorted(DEV_ONLY_OPERATIONS):
            r = await client.request(method, path, headers=h, json=bodies[path])
            assert r.status_code == 404, f"{method} {path} -> {r.status_code} in supabase mode"
        # a dev-signed token (the well-known default dev secret) is worthless in supabase mode
        dev_secret = DEV_JWT_SECRET_DEFAULT
        forged = jwt.encode(claims_for(tenant_a.uid, iss="stockpilot-dev"), dev_secret, algorithm="HS256")
        r = await client.get("/v1/me", headers={"Authorization": f"Bearer {forged}"})
        assert r.status_code == 401


def test_prod_refuses_dev_auth_and_wildcard_cors() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=dev"):
        Settings(_env_file=None, app_env="prod", auth_mode="dev")
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(
            _env_file=None,
            app_env="prod",
            auth_mode="supabase",
            supabase_jwt_secret="s" * 40,
            cors_origins=["*"],
        )
    with pytest.raises(ValueError, match="SUPABASE_JWT_SECRET"):
        Settings(_env_file=None, app_env="prod", auth_mode="supabase")


async def test_prod_hides_api_docs_and_openapi(app_factory: Callable[..., Any]) -> None:
    async with app_factory(
        rate_limit_enabled=False,
        app_env="prod",
        auth_mode="supabase",
        supabase_jwt_secret="s" * 48,
        cors_origins=["https://app.example"],
    ) as client:
        for path in ("/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"):
            assert (await client.get(path)).status_code == 404, path
        assert (await client.post("/v1/auth/dev-login", json={"persona": "priya"})).status_code == 404


def test_prod_refuses_default_dev_credentials() -> None:
    """SEC-02 (fixed): prod will not boot with the public dev DB password, postgres:postgres or a missing issuer."""
    from app.core.config import APP_ROLE_DEV_PASSWORD

    with pytest.raises(ValueError, match=r"(?i)password|default|dev"):
        Settings(
            **prod_kwargs(
                app_db_password=APP_ROLE_DEV_PASSWORD,
                database_url=f"postgresql://stockpilot_app:{APP_ROLE_DEV_PASSWORD}@db/stockpilot",
            )
        )
    with pytest.raises(ValueError, match="DATABASE_ADMIN_URL"):
        Settings(**prod_kwargs(database_admin_url="postgresql://postgres:postgres@db/postgres"))
    with pytest.raises(ValueError, match="JWT_ISSUER"):  # SEC-18
        Settings(**prod_kwargs(jwt_issuer=""))


def test_auth_mode_has_no_silent_dev_default() -> None:
    """SEC-03 (fixed): AUTH_MODE must be chosen explicitly outside prod; prod resolves to supabase."""
    with pytest.raises(ValueError, match="AUTH_MODE is not set"):
        Settings(_env_file=None)
    kw = prod_kwargs()
    del kw["auth_mode"]
    assert Settings(**kw).auth_mode == "supabase"


# ------------------------------------------------------------------------------------------ supabase-mode verifier
def _rsa_pair() -> tuple[Any, str, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return key, pub_pem, jwk


def _hs256_by_hand(claims: dict[str, Any], key: bytes, extra_header: dict[str, Any]) -> str:
    """PyJWT refuses PEM keys as HMAC secrets (good); an attacker would not use PyJWT."""
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT", **extra_header}).encode())
    body = b64(json.dumps(claims).encode())
    sig = hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


def _verifier(settings: Settings, jwks: list[dict[str, Any]] | None, **over: Any) -> TokenVerifier:
    s = settings.model_copy(
        update={"auth_mode": "supabase", "supabase_jwks_url": "https://jwks.example/keys", **over}
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        if jwks is None:
            return httpx.Response(500)
        return httpx.Response(200, json={"keys": jwks})

    return TokenVerifier(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _rejects(v: TokenVerifier, token: str) -> None:
    with pytest.raises(AppError) as ei:
        await v.verify(token)
    assert ei.value.status_code == 401


async def test_jwks_mode_accepts_only_the_registered_key(settings: Settings) -> None:
    key, _pub_pem, jwk = _rsa_pair()
    jwk = {**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}
    v = _verifier(settings, [jwk])
    uid = uuid4()
    good = jwt.encode(claims_for(uid), key, algorithm="RS256", headers={"kid": "k1"})
    assert (await v.verify(good)).user_id == uid

    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    await _rejects(
        v, jwt.encode(claims_for(uid), attacker, algorithm="RS256", headers={"kid": "k1"})
    )  # right kid, wrong key
    await _rejects(
        v, jwt.encode(claims_for(uid), attacker, algorithm="RS256", headers={"kid": "k2"})
    )  # unknown kid
    await _rejects(v, jwt.encode(claims_for(uid), key, algorithm="RS256"))  # no kid
    await _rejects(v, jwt.encode(claims_for(uid), None, algorithm="none", headers={"kid": "k1"}))  # type: ignore[arg-type]  # alg=none
    await _rejects(v, jwt.encode(claims_for(uid, ttl_s=-3600), key, algorithm="RS256", headers={"kid": "k1"}))
    await _rejects(v, jwt.encode(claims_for(uid, aud="anon"), key, algorithm="RS256", headers={"kid": "k1"}))


async def test_jwks_mode_blocks_alg_confusion(settings: Settings) -> None:
    """Classic RS256->HS256 downgrade: HMAC-sign with the *public* key bytes. HS256 is not configured here."""
    _key, pub_pem, jwk = _rsa_pair()
    v = _verifier(settings, [{**jwk, "kid": "k1", "alg": "RS256"}])
    uid = uuid4()
    confused = _hs256_by_hand(claims_for(uid), pub_pem.encode(), {"kid": "k1"})
    await _rejects(v, confused)
    # and when an HS256 secret *is* configured the public key is still not that secret
    v2 = _verifier(settings, [{**jwk, "kid": "k1", "alg": "RS256"}], supabase_jwt_secret="h" * 40)
    await _rejects(v2, confused)


async def test_jwks_outage_is_401_not_500_and_never_accepts(settings: Settings) -> None:
    key, _pub, _jwk = _rsa_pair()
    v = _verifier(settings, None)
    await _rejects(v, jwt.encode(claims_for(uuid4()), key, algorithm="RS256", headers={"kid": "k1"}))


async def test_issuer_is_enforced_when_configured(settings: Settings) -> None:
    secret = "h" * 40
    uid = uuid4()
    v = TokenVerifier(
        settings.model_copy(
            update={
                "auth_mode": "supabase",
                "supabase_jwt_secret": secret,
                "jwt_issuer": "https://p.supabase.co/auth/v1",
            }
        )
    )
    ok = jwt.encode(claims_for(uid, iss="https://p.supabase.co/auth/v1"), secret, algorithm="HS256")
    assert (await v.verify(ok)).user_id == uid
    await _rejects(v, jwt.encode(claims_for(uid, iss="https://evil.example"), secret, algorithm="HS256"))
    await _rejects(v, jwt.encode(claims_for(uid), secret, algorithm="HS256"))  # no iss at all


async def test_aal1_token_of_an_mfa_enrolled_user_is_rejected(settings: Settings) -> None:
    """SEC-04 (fixed): with REQUIRE_MFA (default on in prod) only `aal2` tokens verify."""
    secret = "h" * 40
    uid = uuid4()
    v = TokenVerifier(
        settings.model_copy(
            update={"auth_mode": "supabase", "supabase_jwt_secret": secret, "require_mfa": True}
        )
    )

    def tok(**c: Any) -> str:
        return jwt.encode(claims_for(uid, **c), secret, algorithm="HS256")

    await _rejects(v, tok(aal="aal1", amr=[{"method": "oauth", "timestamp": 1}]))
    await _rejects(v, tok())  # no aal claim at all: fail closed
    await _rejects(v, tok(aal="AAL2"))
    await _rejects(v, tok(aal=["aal2"]))
    ok = await v.verify(tok(aal="aal2", amr=[{"method": "totp", "timestamp": 1}]))
    assert ok.user_id == uid
    # MFA not required (dev/test default): aal1 is accepted
    lax = TokenVerifier(
        settings.model_copy(
            update={"auth_mode": "supabase", "supabase_jwt_secret": secret, "require_mfa": False}
        )
    )
    assert (await lax.verify(tok(aal="aal1"))).user_id == uid


async def test_dev_tokens_carry_aal2_so_require_mfa_works_in_dev(settings: Settings) -> None:
    from app.core.auth import issue_dev_token

    uid = uuid4()
    v = TokenVerifier(settings.model_copy(update={"require_mfa": True}))
    token = issue_dev_token(settings, uid, "a@example.test", now=datetime.now(UTC))
    assert (await v.verify(token)).user_id == uid


async def test_users_row_is_created_only_for_the_verified_subject(
    api: httpx.AsyncClient, db_admin: Any, settings: Settings
) -> None:
    uid = uuid4()
    tok = jwt.encode(claims_for(uid, email="fresh@example.test"), settings.dev_jwt_secret, algorithm="HS256")
    try:
        r = await api.get("/v1/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert await db_admin.fetchval("select email from users where id = $1", uid) == "fresh@example.test"
    finally:
        await db_admin.execute("delete from users where id = $1", uid)


def _ts() -> float:
    return datetime.now(UTC).timestamp()
