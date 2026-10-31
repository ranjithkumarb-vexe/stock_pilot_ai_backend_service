from __future__ import annotations

import httpx

from app.core.config import Settings
from app.main import create_app


async def test_healthz_ok(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers["x-trace-id"]


async def test_readyz_ok_with_real_db(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_503_contract_error_without_db() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://stockpilot_app:x@127.0.0.1:1/nope",
        database_admin_url="postgresql://x:x@127.0.0.1:1/nope",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            live = await client.get("/healthz")
            ready = await client.get("/readyz")
    assert live.status_code == 200  # liveness never depends on the database
    assert ready.status_code == 503
    body = ready.json()
    assert body["code"] == "upstream_unavailable"
    assert body["trace_id"] == ready.headers["x-trace-id"]
    assert set(body) <= {"code", "message", "trace_id", "details"}
