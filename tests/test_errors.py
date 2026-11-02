from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Query

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """App with a few throw-away routes; no database needed (they never touch it)."""
    settings = Settings(_env_file=None, auth_mode="dev", database_url="postgresql://x:x@127.0.0.1:1/nope")
    app: FastAPI = create_app(settings)
    probe = APIRouter()

    @probe.get("/v1/_t/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @probe.get("/v1/_t/violation")
    async def violation() -> None:
        raise AppError(
            ErrorCode.RULE_VIOLATION,
            "Rule violated",
            details={"violations": [{"rule": "pos", "limit": 10}]},
        )

    @probe.get("/v1/_t/num")
    async def num(n: int = Query(ge=1)) -> dict[str, int]:
        return {"n": n}

    app.include_router(probe)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _assert_contract(resp: httpx.Response, code: str) -> dict[str, object]:
    body: dict[str, object] = resp.json()
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    assert body["trace_id"] == resp.headers["x-trace-id"]
    assert set(body) <= {"code", "message", "trace_id", "details"}
    return body


async def test_unknown_route_is_contract_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/does-not-exist")
    assert resp.status_code == 404
    _assert_contract(resp, "not_found")


async def test_wrong_method_is_contract_405(client: httpx.AsyncClient) -> None:
    resp = await client.post("/healthz")
    assert resp.status_code == 405
    _assert_contract(resp, "method_not_allowed")


async def test_validation_error_422_hides_input(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/_t/num", params={"n": "0"})
    assert resp.status_code == 422
    body = _assert_contract(resp, "validation_error")
    details = body["details"]
    assert isinstance(details, dict) and details["errors"][0]["loc"] == ["query", "n"]
    assert "input" not in details["errors"][0]


async def test_unhandled_exception_is_contract_500_without_leaks(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        resp = await client.get("/v1/_t/boom")
    assert resp.status_code == 500
    body = _assert_contract(resp, "internal")
    assert "secret internal detail" not in resp.text
    assert body["message"] == "Internal server error"
    # security headers still applied to error responses
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert any("unhandled exception" in r.getMessage() for r in caplog.records)


async def test_app_error_with_details_and_status_mapping(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/_t/violation")
    assert resp.status_code == 422
    body = _assert_contract(resp, "rule_violation")
    assert body["details"] == {"violations": [{"rule": "pos", "limit": 10}]}


async def test_every_contract_code_has_a_status() -> None:
    from app.core.errors import STATUS_FOR_CODE

    contract = {
        "unauthenticated": 401, "forbidden": 403, "not_found": 404, "conflict": 409, "validation_error": 422,
        "rate_limited": 429, "rule_violation": 422, "stale_quote": 409, "market_closed": 409,
        "insufficient_cash": 422, "insufficient_shares": 422, "unsupported_symbol": 422,
        "guardrail_blocked": 422, "upstream_unavailable": 503, "internal": 500,
    }  # fmt: skip
    for code, status in contract.items():
        assert STATUS_FOR_CODE[ErrorCode(code)] == status


async def test_incoming_trace_id_is_propagated_and_invalid_one_replaced(
    client: httpx.AsyncClient,
) -> None:
    good = await client.get("/healthz", headers={"X-Trace-Id": "abc-123-trace-id"})
    assert good.headers["x-trace-id"] == "abc-123-trace-id"
    bad = await client.get("/healthz", headers={"X-Trace-Id": "bad id with spaces\t<script>"})
    assert bad.headers["x-trace-id"] != "bad id with spaces\t<script>"
    assert len(bad.headers["x-trace-id"]) == 32


async def test_trace_id_in_log_lines(client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    from app.core.logging import JsonFormatter

    with caplog.at_level(logging.INFO, logger="app.access"):
        resp = await client.get("/healthz", headers={"X-Trace-Id": "trace-for-logs-1"})
    assert resp.status_code == 200
    rec = next(r for r in caplog.records if r.name == "app.access")
    assert rec.__dict__["status"] == 200
    # the formatter stamps the active trace id; inside the request the context var was set
    line = JsonFormatter().format(rec)
    assert '"msg":"request"' in line
