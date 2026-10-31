from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.core.clock import FakeClock
from app.core.config import Settings
from app.main import create_app


def _offline_settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://x:x@127.0.0.1:1/nope", **kw)


async def _client(**kw: Any) -> tuple[Any, httpx.AsyncClient]:
    clock = kw.pop("clock", None)
    app = create_app(_offline_settings(**kw), clock=clock)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_security_headers_on_every_response() -> None:
    app, client = await _client()
    async with app.router.lifespan_context(app), client:
        for path in ("/healthz", "/nope"):
            r = await client.get(path)
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert "max-age" in r.headers["strict-transport-security"]
            assert "default-src 'none'" in r.headers["content-security-policy"]
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["x-trace-id"]


async def test_cors_allowlist() -> None:
    app, client = await _client(cors_origins="http://localhost:5173")
    async with app.router.lifespan_context(app), client:
        ok = await client.get("/healthz", headers={"Origin": "http://localhost:5173"})
        assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "x-trace-id" in ok.headers["access-control-expose-headers"].lower()
        denied = await client.get("/healthz", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in denied.headers
        pre = await client.options(
            "/v1/me",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert pre.status_code == 200
        assert pre.headers["x-trace-id"]  # even preflights are traceable
        assert "authorization" in pre.headers["access-control-allow-headers"].lower()


async def test_cors_disabled_by_default() -> None:
    app, client = await _client()
    async with app.router.lifespan_context(app), client:
        r = await client.get("/healthz", headers={"Origin": "http://localhost:5173"})
        assert "access-control-allow-origin" not in r.headers


async def test_ip_rate_limit_429_retry_after_and_refill() -> None:
    clock = FakeClock()
    app, client = await _client(rate_limit_ip_burst=3, rate_limit_ip_per_s=1.0, clock=clock)
    async with app.router.lifespan_context(app), client:
        statuses = [(await client.get("/v1/nothing")).status_code for _ in range(3)]
        assert statuses == [404, 404, 404]
        limited = await client.get("/v1/nothing")
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1
        body = limited.json()
        assert body["code"] == "rate_limited"
        assert body["trace_id"] == limited.headers["x-trace-id"]
        assert limited.headers["x-content-type-options"] == "nosniff"
        # health probes are exempt even while limited
        assert (await client.get("/healthz")).status_code == 200
        clock.advance(2)  # refills 2 tokens
        assert (await client.get("/v1/nothing")).status_code == 404


async def test_rate_limit_can_be_disabled() -> None:
    app, client = await _client(rate_limit_enabled=False, rate_limit_ip_burst=1)
    async with app.router.lifespan_context(app), client:
        codes = [(await client.get("/v1/nothing")).status_code for _ in range(5)]
        assert codes == [404] * 5


async def test_x_forwarded_for_only_trusted_behind_proxy() -> None:
    app, client = await _client(rate_limit_ip_burst=2, rate_limit_ip_per_s=0.001, trust_proxy=True)
    async with app.router.lifespan_context(app), client:
        # different forwarded clients get separate buckets when TRUST_PROXY=true
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            for _ in range(2):
                assert (await client.get("/v1/x", headers={"X-Forwarded-For": ip})).status_code == 404
    app2, client2 = await _client(rate_limit_ip_burst=2, rate_limit_ip_per_s=0.001, trust_proxy=False)
    async with app2.router.lifespan_context(app2), client2:
        codes = [
            (await client2.get("/v1/x", headers={"X-Forwarded-For": f"9.9.9.{i}"})).status_code
            for i in range(3)
        ]
        assert codes == [404, 404, 429]  # spoofed header ignored


async def test_body_size_cap_content_length_and_streaming() -> None:
    app, client = await _client(max_body_bytes=100)
    async with app.router.lifespan_context(app), client:
        big = await client.post(
            "/v1/auth/dev-login", content=b"x" * 500, headers={"content-type": "application/json"}
        )
        assert big.status_code == 413
        assert big.json()["code"] == "payload_too_large"

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(10):
                yield b"y" * 50

        streamed = await client.post(
            "/v1/auth/dev-login", content=chunks(), headers={"content-type": "application/json"}
        )
        assert streamed.status_code == 413
        small = await client.post("/v1/auth/dev-login", json={"persona": "bogus"})
        assert small.status_code == 422  # reaches the app, rejected by validation (no DB involved)


async def test_access_log_never_contains_query_string_or_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, client = await _client()
    async with app.router.lifespan_context(app), client:
        with caplog.at_level(logging.INFO, logger="app.access"):
            await client.get(
                "/v1/stream?token=SUPERSECRETTOKEN", headers={"Authorization": "Bearer SECRETJWT"}
            )
    records = [r for r in caplog.records if r.name.startswith("app.")]  # our loggers (httpx logs its own URL)
    blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in records)
    assert "SUPERSECRETTOKEN" not in blob
    assert "SECRETJWT" not in blob
    assert any(r.__dict__.get("path") == "/v1/stream" for r in records)


async def test_user_rate_limit_applies_after_auth(
    app_factory: Callable[..., Any], make_user: Callable[..., Any], auth_headers: Callable[..., Any]
) -> None:
    a = await make_user()
    b = await make_user()
    async with app_factory(rate_limit_user_burst=2, rate_limit_user_per_s=0.001) as client:
        for _ in range(2):
            assert (await client.get("/v1/me", headers=auth_headers(a))).status_code == 200
        limited = await client.get("/v1/me", headers=auth_headers(a))
        assert limited.status_code == 429
        assert limited.json()["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) >= 1
        # another user is unaffected
        assert (await client.get("/v1/me", headers=auth_headers(b))).status_code == 200


async def test_error_responses_keep_cors_headers_so_browsers_can_read_them() -> None:
    app, client = await _client(
        cors_origins="http://localhost:5173", rate_limit_ip_burst=1, rate_limit_ip_per_s=0.001
    )
    async with app.router.lifespan_context(app), client:
        origin = {"Origin": "http://localhost:5173"}
        await client.get("/v1/nothing", headers=origin)
        limited = await client.get("/v1/nothing", headers=origin)
        assert limited.status_code == 429
        assert limited.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert limited.headers["x-trace-id"]
