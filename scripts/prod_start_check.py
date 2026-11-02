"""Production-mode start-up check without Docker (BE-21):  `make prod-check`  or  `python -m scripts.prod_start_check`.

Creates a throw-away database on the server in DATABASE_ADMIN_URL (default name `stockpilot_prodcheck`), migrates it with a
random APP_DB_PASSWORD, starts uvicorn exactly as the container does (`APP_ENV=prod AUTH_MODE=supabase`, fake secrets, no
dev auth) and probes it:

  /healthz, /readyz = 200 | dev-login, /docs, /openapi.json = 404 | /v1/me: no token 401, forged token 401, aal1 (no MFA)
  token 401, valid Supabase-style aal2 HS256 token 200 | CORS: the configured origin is allowed, a foreign origin is not |
  security headers | the process REFUSES to start with the dev DB password, postgres:postgres, no JWT_ISSUER, AUTH_MODE=dev
  or a placeholder SEC_USER_AGENT (SEC-02/03/18) | X-Forwarded-For spoofing cannot dodge the per-IP limit (SEC-08).

Exit 0 only if every probe passes. Everything it creates is dropped again (`--keep` leaves the database).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg
import httpx
import jwt

from app.core.config import dsn_with_database, dsn_with_user, get_settings
from app.core.migrate import ensure_database, migrate

DB_NAME = "stockpilot_prodcheck"
ADMIN_ROLE = "sp_prodcheck_admin"  # throw-away superuser: prod mode refuses the well-known postgres:postgres admin credentials
ORIGIN = "https://stockpilot-web.example.vercel.app"
ISSUER = "https://fakeprojectref.supabase.co/auth/v1"


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str


async def _drop(admin_dsn: str) -> None:
    conn = await asyncpg.connect(dsn_with_database(admin_dsn, "postgres"))
    try:
        await conn.execute(f'drop database if exists "{DB_NAME}" with (force)')
    finally:
        await conn.close()


def _probes(base: str, jwt_secret: str) -> list[Probe]:
    results: list[Probe] = []

    def check(name: str, cond: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = cond()
        except Exception as exc:  # a probe that raises is a failed probe
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(Probe(name, ok, detail))

    with httpx.Client(base_url=base, timeout=10.0) as c:
        check(
            "GET /healthz -> 200", lambda: ((r := c.get("/healthz")).status_code == 200, str(r.status_code))
        )
        check("GET /readyz -> 200", lambda: ((r := c.get("/readyz")).status_code == 200, str(r.status_code)))
        check(
            "POST /v1/auth/dev-login -> 404 (dev auth is off)",
            lambda: (
                (r := c.post("/v1/auth/dev-login", json={"persona": "priya"})).status_code == 404,
                str(r.status_code),
            ),
        )
        check("GET /docs -> 404", lambda: ((r := c.get("/docs")).status_code == 404, str(r.status_code)))
        check(
            "GET /openapi.json -> 404",
            lambda: ((r := c.get("/openapi.json")).status_code == 404, str(r.status_code)),
        )
        check(
            "GET /v1/me without token -> 401",
            lambda: ((r := c.get("/v1/me")).status_code == 401, str(r.status_code)),
        )
        forged = jwt.encode(
            {"sub": str(uuid.uuid4()), "aud": "authenticated", "iss": ISSUER, "exp": int(time.time()) + 600},
            "x" * 48,
            algorithm="HS256",
        )
        check(
            "GET /v1/me with a token signed by the wrong secret -> 401",
            lambda: (
                (r := c.get("/v1/me", headers={"Authorization": f"Bearer {forged}"})).status_code == 401,
                str(r.status_code),
            ),
        )
        aal1 = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "aud": "authenticated",
                "iss": ISSUER,
                "exp": int(time.time()) + 600,
                "aal": "aal1",
            },
            jwt_secret,
            algorithm="HS256",
        )
        check(
            "GET /v1/me with a correctly signed aal1 token (no MFA) -> 401 (SEC-04)",
            lambda: (
                (r := c.get("/v1/me", headers={"Authorization": f"Bearer {aal1}"})).status_code == 401,
                str(r.status_code),
            ),
        )
        good = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "aud": "authenticated",
                "iss": ISSUER,
                "exp": int(time.time()) + 600,
                "email": "prod.check@example.com",
                "aal": "aal2",
            },
            jwt_secret,
            algorithm="HS256",
        )
        check(
            "GET /v1/me with a valid Supabase-style aal2 token -> 200",
            lambda: (
                (r := c.get("/v1/me", headers={"Authorization": f"Bearer {good}"})).status_code == 200,
                str(r.status_code),
            ),
        )
        check(
            "CORS preflight from the configured web origin is allowed",
            lambda: (
                (
                    r := c.options(
                        "/v1/me", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
                    )
                ).headers.get("access-control-allow-origin")
                == ORIGIN,
                str(r.status_code),
            ),
        )
        check(
            "CORS preflight from a foreign origin is refused",
            lambda: (
                "access-control-allow-origin"
                not in (
                    r := c.options(
                        "/v1/me",
                        headers={
                            "Origin": "https://evil.example.org",
                            "Access-Control-Request-Method": "GET",
                        },
                    )
                ).headers,
                str(r.status_code),
            ),
        )
        check(
            "security headers (nosniff, HSTS, frame-ancestors none)",
            lambda: (
                (h := c.get("/healthz").headers).get("x-content-type-options") == "nosniff"
                and "max-age" in h.get("strict-transport-security", "")
                and "frame-ancestors 'none'" in h.get("content-security-policy", ""),
                "",
            ),
        )
    return results


def _rate_limit_probe(base: str) -> list[Probe]:
    """SEC-08: with one trusted hop the LAST X-Forwarded-For entry keys the bucket; rotating the left side does nothing."""
    with httpx.Client(base_url=base, timeout=10.0) as c:
        codes = [
            c.get("/v1/universe", headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.99"}).status_code
            for i in range(60)
        ]
        other = c.get("/v1/universe", headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.100"}).status_code
    return [
        Probe(
            "rotating the left side of X-Forwarded-For does not dodge the per-IP limit (burst 30)",
            429 in codes and other == 200,
            f"429 seen={429 in codes}, other client={other}",
        )
    ]


async def _prepare(admin_dsn: str, app_password: str, admin_role_password: str) -> None:
    await ensure_database(admin_dsn, DB_NAME)
    await migrate(dsn_with_database(admin_dsn, DB_NAME), app_password=app_password)
    conn = await asyncpg.connect(dsn_with_database(admin_dsn, "postgres"))
    try:
        await conn.execute(f'drop role if exists "{ADMIN_ROLE}"')
        await conn.execute(f"create role \"{ADMIN_ROLE}\" superuser login password '{admin_role_password}'")
    finally:
        await conn.close()


async def _drop_role(admin_dsn: str) -> None:
    conn = await asyncpg.connect(dsn_with_database(admin_dsn, "postgres"))
    try:
        await conn.execute(f'drop role if exists "{ADMIN_ROLE}"')
    finally:
        await conn.close()


def _refusals(env: dict[str, str]) -> list[Probe]:
    """Each broken variable must make `Settings()` fail in a fresh interpreter (= the container would not start)."""
    cases: dict[str, dict[str, str]] = {
        "dev DB password (APP_DB_PASSWORD=stockpilot_dev_pw)": {"APP_DB_PASSWORD": "stockpilot_dev_pw"},
        "postgres:postgres admin URL": {
            "DATABASE_ADMIN_URL": "postgresql://postgres:postgres@db.example/postgres"
        },
        "missing JWT_ISSUER": {"JWT_ISSUER": ""},
        "AUTH_MODE=dev": {"AUTH_MODE": "dev"},
        "placeholder SEC_USER_AGENT": {"SEC_USER_AGENT": "StockPilot <your-email>"},
        "LLM_PROVIDER=meta without a key": {"LLM_PROVIDER": "meta", "META_MODEL_API_KEY": ""},
    }
    out: list[Probe] = []
    for name, over in cases.items():
        proc = subprocess.run(
            [sys.executable, "-c", "from app.core.config import Settings; Settings()"],
            env={**env, **over},
            capture_output=True,
            text=True,
            timeout=60,
        )
        said = proc.stderr + proc.stdout
        refused = proc.returncode != 0 and (
            "refuses to start" in said or "AUTH_MODE=dev is not allowed" in said
        )
        out.append(Probe(f"refuses to start with {name}", refused, f"exit {proc.returncode}"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Boot the API in production mode against a scratch database and probe it"
    )
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--keep", action="store_true", help="do not drop the scratch database afterwards")
    args = ap.parse_args(argv)

    admin_dsn = get_settings().database_admin_url
    app_password = secrets.token_urlsafe(24)
    jwt_secret = secrets.token_urlsafe(48)
    admin_role_password = secrets.token_urlsafe(24)
    asyncio.run(_prepare(admin_dsn, app_password, admin_role_password))
    scratch_admin = dsn_with_user(dsn_with_database(admin_dsn, DB_NAME), ADMIN_ROLE, admin_role_password)
    env = {
        **os.environ,
        "APP_ENV": "prod",
        "AUTH_MODE": "supabase",
        "SUPABASE_URL": "https://fakeprojectref.supabase.co",
        "SUPABASE_JWT_SECRET": jwt_secret,
        "JWT_ISSUER": ISSUER,
        "CORS_ORIGINS": ORIGIN,
        "TRUST_PROXY": "true",
        "TRUSTED_PROXY_HOPS": "1",
        "RATE_LIMIT_IP_BURST": "30",
        "RATE_LIMIT_IP_PER_S": "0.001",
        "MARKETDATA_AUTOSTART": "0",
        "DATABASE_ADMIN_URL": scratch_admin,
        "DATABASE_URL": dsn_with_user(scratch_admin, "stockpilot_app", app_password),
        "APP_DB_PASSWORD": app_password,
        "LLM_PROVIDER": "mock",
        "MARKET_DATA_SOURCE": "simulated",
        "ALERTS_EDGAR_LIVE": "0",
        "SEC_USER_AGENT": "StockPilot prod-check@example.com",
    }
    server = subprocess.Popen(  # noqa: S603 - fixed argv, our own module
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{args.port}"
    try:
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline and server.poll() is None:
            try:
                if httpx.get(f"{base}/readyz", timeout=2.0).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not ready:
            print(
                "prod-check: FAIL - the server did not become ready within 30 s (exit code:",
                server.poll(),
                ")",
            )
            return 1
        results = _probes(base, jwt_secret)
        results += _rate_limit_probe(base)
        results += _refusals(env)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        if not args.keep:
            asyncio.run(_drop(admin_dsn))
            asyncio.run(_drop_role(admin_dsn))
    for p in results:
        print(f"{'ok  ' if p.ok else 'FAIL'} {p.name}" + (f"  [{p.detail}]" if p.detail and not p.ok else ""))
    failed = [p for p in results if not p.ok]
    print(
        f"prod-check: {'FAILED ' + str(len(failed)) + ' probe(s)' if failed else 'OK, ' + str(len(results)) + ' probes'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
