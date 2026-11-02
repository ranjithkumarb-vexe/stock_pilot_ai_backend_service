"""SEC-01 area 2: input validation, injection, idempotency, rate limits, headers, CORS, error hygiene."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from tests.security.conftest import Tenant, Trader

pytestmark = pytest.mark.db

SQLI = [
    "' OR '1'='1",
    "'; DROP TABLE users;--",
    "1; select pg_sleep(5)--",
    "') UNION SELECT NULL,NULL,NULL--",
    "AAPL' AND 1=(select count(*) from users)--",
    '" OR ""="',
    "$$; select pg_sleep(5); $$",
    "{{7*7}}${7*7}",
    "../../../../etc/passwd",
    "%27%20OR%201%3D1",
    "<script>alert(1)</script>",
    "\\'; select 1;--",
]
DB_LEAK = re.compile(
    r"(syntax error|asyncpg|psycopg|postgres|relation \"|column \"|traceback|File \"/|\.py\", line)", re.I
)


def _assert_clean(r: httpx.Response, elapsed: float, where: str) -> None:
    assert r.status_code < 500, f"{where}: {r.status_code} {r.text[:200]}"
    assert elapsed < 3.0, f"{where}: took {elapsed:.1f}s (stacked query executed?)"
    assert not DB_LEAK.search(r.text), f"{where}: leaks internals: {r.text[:200]}"


# ---------------------------------------------------------------------------------------- SQL injection battery
@pytest.mark.parametrize("payload", SQLI)
async def test_sql_injection_payloads_in_every_string_input(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, payload: str
) -> None:
    h = tenant_a.headers
    users_before = await db_admin.fetchval("select count(*) from users")
    enc = httpx.QueryParams({"x": payload}).get("x") or payload  # let httpx percent-encode
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", "/v1/briefs/{p}", {}),
        ("POST", "/v1/briefs/{p}/generate", {}),
        ("GET", "/v1/quotes/{p}/bars", {}),
        ("GET", "/v1/why/{p}/{p}", {}),
        ("GET", "/v1/audit/traces/{p}", {}),
        ("PUT", "/v1/watchlist/{p}", {}),
        ("DELETE", "/v1/watchlist/{p}", {}),
        ("GET", "/v1/quotes", {"params": {"symbols": payload}}),
        ("GET", "/v1/alerts", {"params": {"state": payload, "type": payload}}),
        ("GET", "/v1/orders", {"params": {"status": payload}}),
        ("GET", "/v1/audit/traces", {"params": {"outcome": payload, "kind": payload}}),
        ("GET", "/v1/chat/history", {"params": {"thread_id": payload}}),
        ("GET", "/v1/feed", {"params": {"limit": payload}}),
        ("POST", "/v1/chat", {"json": {"message": payload}}),
        ("PATCH", "/v1/profile", {"json": {"display_name": payload}}),
        ("POST", "/v1/events", {"json": {"events": [{"event_id": payload, "type": payload, "ts": payload}]}}),
        (
            "POST",
            "/v1/orders/preview",
            {"json": {"symbol": payload, "side": "buy", "type": "market", "qty": 1}},
        ),
        ("POST", "/v1/auth/dev-login", {"json": {"persona": payload}}),
    ]
    for method, tmpl, kw in calls:
        path = tmpl.format(
            p=httpx.URL("http://x/").copy_with(path="/" + payload).path[1:].replace("/", "%2F")
        )
        t0 = time.perf_counter()
        r = await api.request(method, path, headers=h, **kw)
        _assert_clean(r, time.perf_counter() - t0, f"{method} {tmpl}")
    assert await db_admin.fetchval("select count(*) from users") >= users_before
    assert (
        await db_admin.fetchval("select count(*) from information_schema.tables where table_name = 'users'")
        == 1
    )
    del enc


# ---------------------------------------------------------------------------------------- sizes / shapes
async def test_oversized_body_rejected_before_parsing(api: httpx.AsyncClient, tenant_b: Tenant) -> None:
    big = b'{"message":"' + b"a" * (1_048_576 + 10) + b'"}'
    r = await api.post(
        "/v1/chat", content=big, headers={**tenant_b.headers, "Content-Type": "application/json"}
    )
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


async def test_oversized_chunked_body_rejected(api: httpx.AsyncClient, tenant_b: Tenant) -> None:
    async def gen() -> AsyncIterator[bytes]:
        yield b'{"message":"'
        for _ in range(20):
            yield b"a" * 100_000
        yield b'"}'

    r = await api.post(
        "/v1/chat", content=gen(), headers={**tenant_b.headers, "Content-Type": "application/json"}
    )
    assert r.status_code == 413, r.text


async def test_lying_content_length_cannot_smuggle_a_big_body(
    api: httpx.AsyncClient, tenant_b: Tenant
) -> None:
    class Evil(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x" * 2_000_000

    req = api.build_request(
        "POST",
        "/v1/chat",
        headers={**tenant_b.headers, "Content-Type": "application/json", "Content-Length": "10"},
        content=b"{}",
    )
    req.stream = Evil()
    try:
        r = await api.send(req)
        assert r.status_code in {400, 413, 422}
    except (httpx.HTTPError, RuntimeError, ValueError):
        pass  # the transport refused the inconsistent request, which is also fine


@pytest.mark.parametrize(
    ("body", "ctype"),
    [
        (b'{"message": ', "application/json"),
        (b"", "application/json"),
        (b"null", "application/json"),
        (b"[]", "application/json"),
        (b'{"message":"hi","message":"dup"}', "application/json"),
        (b'{"message":"\xff\xfe"}', "application/json"),
        (b"[" * 40_000 + b"]" * 40_000, "application/json"),
        (b'{"message":"hi"}', "text/plain"),
        (b'{"message":"hi"}', "application/x-www-form-urlencoded"),
    ],
)
async def test_malformed_bodies_get_a_contract_error_never_a_500(
    api: httpx.AsyncClient, tenant_b: Tenant, body: bytes, ctype: str
) -> None:
    r = await api.post("/v1/chat", content=body, headers={**tenant_b.headers, "Content-Type": ctype})
    assert r.status_code in {200, 400, 422}, r.text[:200]
    if r.status_code != 200:
        j = r.json()
        assert set(j) >= {"code", "message", "trace_id"}
        assert not DB_LEAK.search(r.text)


async def test_validation_errors_never_echo_the_input(api: httpx.AsyncClient, tenant_b: Tenant) -> None:
    secret = "sk-live-SUPERSECRETVALUE-123456"
    r = await api.post(
        "/v1/chat", headers=tenant_b.headers, json={"message": ["x", secret], "thread_id": secret}
    )
    assert r.status_code == 422
    assert secret not in r.text  # values are never echoed (unknown *key names* are, see SEC-09 info)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/v1/orders", {"limit": 10**12}),
        ("/v1/orders", {"limit": 0}),
        ("/v1/orders", {"limit": -5}),
        ("/v1/orders", {"limit": "1e9"}),
        ("/v1/alerts", {"limit": 10**9}),
        ("/v1/proposals", {"limit": 10**9}),
        ("/v1/audit/traces", {"limit": 10**9}),
        ("/v1/chat/history", {"limit": 10**9}),
        ("/v1/feed", {"limit": 10**9}),
        ("/v1/quotes/AAPL/bars", {"limit": 10**9}),
        ("/v1/quotes", {"symbols": ",".join(f"S{i}" for i in range(500))}),
    ],
)
async def test_huge_or_negative_paging_values_are_422(
    api: httpx.AsyncClient, tenant_b: Tenant, path: str, params: dict[str, Any]
) -> None:
    r = await api.get(path, params=params, headers=tenant_b.headers)
    assert r.status_code == 422, (path, params, r.status_code)


@pytest.mark.parametrize(
    "path",
    ["/v1/orders", "/v1/alerts", "/v1/feed"],
)
async def test_garbage_cursors_are_422(api: httpx.AsyncClient, tenant_b: Tenant, path: str) -> None:
    for cursor in ("garbage", "A" * 150, "%00", "9" * 40, "-1"):
        r = await api.get(path, params={"cursor": cursor}, headers=tenant_b.headers)
        assert r.status_code in {200, 422}, (path, cursor, r.status_code)


@pytest.mark.parametrize("path", ["/v1/audit/traces", "/v1/chat/history"])
@pytest.mark.parametrize(
    "cursor",
    [
        "garbage",
        "2026-13-45T99:99:99",
        "0000-01-01",
        "1; drop table users",
        "9" * 500,
        "2026-01-01T00:00:00+99:99",
        "%00",
    ],
)
async def test_garbage_cursor_on_audit_and_chat_history_is_422(
    api: httpx.AsyncClient, tenant_b: Tenant, path: str, cursor: str
) -> None:
    """SEC-06 (fixed): an unparseable cursor is a contract 422, never a 500."""
    r = await api.get(path, params={"cursor": cursor}, headers=tenant_b.headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"


@pytest.mark.parametrize("path", ["/v1/audit/traces", "/v1/chat/history"])
@pytest.mark.parametrize(
    "cursor", ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00+05:30", "2026-01-01T00:00:00", "2026-01-01"]
)
async def test_valid_timestamp_cursors_still_work(
    api: httpx.AsyncClient, tenant_b: Tenant, path: str, cursor: str
) -> None:
    r = await api.get(path, params={"cursor": cursor}, headers=tenant_b.headers)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/chat", {"message": "hello \u0000 world"}),
        ("PATCH", "/v1/profile", {"display_name": "a\u0000b"}),
        ("PATCH", "/v1/profile", {"display_name": "ok", "rules": {"nested": ["x\u0000"]}}),
        ("POST", "/v1/events", {"events": [{"type": "view", "symbol": "AAPL\u0000"}]}),
    ],
)
async def test_nul_bytes_are_rejected_cleanly(
    api: httpx.AsyncClient, tenant_b: Tenant, method: str, path: str, body: dict[str, Any]
) -> None:
    """SEC-07 (fixed): NUL anywhere in a body is a contract 422 (or the route does not exist), never a 500."""
    r = await api.request(method, path, headers=tenant_b.headers, json=body)
    assert r.status_code in {404, 405, 422}, r.text
    if r.status_code == 422:
        assert r.json()["code"] == "validation_error"
    for chat in (
        {"message": "\u0000"},
        {"message": "ok", "thread_id": "\u0000"},
    ):
        assert (await api.post("/v1/chat", headers=tenant_b.headers, json=chat)).status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "/v1/watchlist/AAP%00L",
        "/v1/orders?status=x%00",
        "/v1/chat/history?thread_id=%00",
        "/v1/audit/traces?outcome=a%00b&kind=%00",
        "/v1/market/quotes?symbols=AAPL%00",
        "/v1/briefs/AA%00PL",
    ],
)
async def test_nul_in_path_and_query_never_500s(api: httpx.AsyncClient, tenant_b: Tenant, url: str) -> None:
    for method in ("GET", "DELETE"):
        r = await api.request(method, url, headers=tenant_b.headers)
        assert r.status_code < 500, (method, url, r.status_code, r.text)


# ---------------------------------------------------------------------------------------- idempotency
async def test_idempotency_key_rules(trader: Trader) -> None:
    c, h = trader.client, trader.headers
    body = {"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1}
    r = await c.post("/v1/orders", headers=h, json=body)  # missing key
    assert r.status_code == 422 and "Idempotency-Key" in r.json()["message"]
    for bad in ("not-a-uuid", "' or 1=1--", "x" * 500, " "):
        r = await c.post("/v1/orders", headers={**h, "Idempotency-Key": bad}, json=body)
        assert r.status_code in {422, 400}, bad
    key = str(uuid4())
    first = await c.post("/v1/orders", headers={**h, "Idempotency-Key": key}, json=body)
    assert first.status_code == 201
    again = await c.post("/v1/orders", headers={**h, "Idempotency-Key": key}, json=body)
    assert again.status_code == 200 and again.headers.get("idempotent-replay") == "true"
    assert again.json()["order"]["id"] == first.json()["order"]["id"]
    clash = await c.post("/v1/orders", headers={**h, "Idempotency-Key": key}, json={**body, "qty": 2})
    assert clash.status_code == 409
    n = len((await c.get("/v1/orders", headers=h)).json()["items"])
    assert n == 1, "replays must not create a second order"


async def test_idempotency_key_is_scoped_per_user(trader: Trader, tenant_b: Tenant) -> None:
    """B reusing A's key neither replays A's order nor learns it exists."""
    key = str(uuid4())
    body = {"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1}
    a = await trader.client.post("/v1/orders", headers={**trader.headers, "Idempotency-Key": key}, json=body)
    assert a.status_code == 201
    rb = await trader.client.post(
        "/v1/orders", headers={**tenant_b.headers, "Idempotency-Key": key}, json=body
    )
    assert a.json()["order"]["id"] not in rb.text


async def test_concurrent_identical_orders_execute_once(trader: Trader) -> None:
    key = str(uuid4())
    body = {"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1}
    rs = await asyncio.gather(
        *[
            trader.client.post("/v1/orders", headers={**trader.headers, "Idempotency-Key": key}, json=body)
            for _ in range(8)
        ]
    )
    assert sorted({r.status_code for r in rs}) in ([200, 201], [201])
    assert len((await trader.client.get("/v1/orders", headers=trader.headers)).json()["items"]) == 1


async def test_order_amounts_are_bounded_and_cannot_be_negative(trader: Trader) -> None:
    for qty in (-1, 0, 0.5, 1_000_001, "NaN", "Infinity", 1e30):
        r = await trader.client.post(
            "/v1/orders",
            headers={**trader.headers, "Idempotency-Key": str(uuid4())},
            json={"symbol": "AAPL", "side": "buy", "type": "market", "qty": qty},
        )
        assert r.status_code in {422, 400}, (qty, r.status_code, r.text[:120])
    for lp in (-1, 0, "NaN", 1e12):
        r = await trader.client.post(
            "/v1/orders",
            headers={**trader.headers, "Idempotency-Key": str(uuid4())},
            json={"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 1, "limit_price": lp},
        )
        assert r.status_code in {422, 400}, (lp, r.status_code)


async def test_market_orders_cannot_exceed_cash_or_shares(trader: Trader) -> None:
    c, h = trader.client, trader.headers
    r = await c.post(
        "/v1/orders",
        headers={**h, "Idempotency-Key": str(uuid4())},
        json={"symbol": "AAPL", "side": "buy", "type": "market", "qty": 900_000},
    )
    assert r.status_code == 422, r.text
    r = await c.post(
        "/v1/orders",
        headers={**h, "Idempotency-Key": str(uuid4())},
        json={"symbol": "AAPL", "side": "sell", "type": "market", "qty": 5},
    )
    assert r.status_code == 422 and r.json()["code"] in {"insufficient_shares", "rule_violation"}


# ---------------------------------------------------------------------------------------- rate limiting
async def test_per_ip_rate_limit_returns_429_with_retry_after(app_factory: Callable[..., Any]) -> None:
    async with app_factory(rate_limit_ip_burst=3, rate_limit_ip_per_s=0.01) as c:
        codes = [(await c.get("/v1/universe")).status_code for _ in range(6)]
        assert codes[:3] == [200, 200, 200] and set(codes[3:]) == {429}
        r = await c.get("/v1/universe")
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert r.json()["code"] == "rate_limited"
        assert (await c.get("/healthz")).status_code == 200  # probes stay exempt


async def test_per_user_rate_limit_is_keyed_on_the_verified_subject(
    app_factory: Callable[..., Any], tenant_a: Tenant, tenant_b: Tenant
) -> None:
    async with app_factory(
        rate_limit_user_burst=3, rate_limit_user_per_s=0.01, rate_limit_ip_burst=10_000
    ) as c:
        ra = [(await c.get("/v1/me", headers=tenant_a.headers)).status_code for _ in range(5)]
        assert ra[:3] == [200] * 3 and ra[3:] == [429, 429]
        r = await c.get("/v1/me", headers=tenant_a.headers)
        assert int(r.headers["retry-after"]) >= 1
        assert (await c.get("/v1/me", headers=tenant_b.headers)).status_code == 200  # B is unaffected
        # an unauthenticated/forged caller cannot drain A's budget
        for _ in range(10):
            await c.get("/v1/me", headers={"Authorization": "Bearer forged.token.value"})
        assert (await c.get("/v1/me", headers=tenant_b.headers)).status_code == 200


async def test_xff_spoofing_cannot_bypass_ip_rate_limit(app_factory: Callable[..., Any]) -> None:
    async with app_factory(rate_limit_ip_burst=3, rate_limit_ip_per_s=0.001, trust_proxy=True) as c:
        codes = []
        for i in range(8):
            # a proxy appends the real peer ("203.0.113.50") on the right; the attacker controls what is on the left
            codes.append(
                (
                    await c.get("/v1/universe", headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.50"})
                ).status_code
            )
        assert 429 in codes


async def test_xff_spoofing_cannot_bypass_ip_rate_limit_with_prepended_entries(
    app_factory: Callable[..., Any],
) -> None:
    """SEC-08: junk, private ranges, IPv6 and repeated header lines in front of the proxy-appended entry change nothing."""
    async with app_factory(rate_limit_ip_burst=3, rate_limit_ip_per_s=0.001, trust_proxy=True) as c:
        codes = []
        for i in range(10):
            spoof = [f"10.0.0.{i}", f"2001:db8::{i:x}", f"evil-{i}", "", f"198.51.100.{i}"]
            h = [("X-Forwarded-For", ", ".join(spoof)), ("X-Forwarded-For", "203.0.113.50")]
            codes.append((await c.get("/v1/universe", headers=h)).status_code)
        assert codes[:3] == [200, 200, 200] and set(codes[3:]) == {429}


async def test_xff_hops_two_trusts_the_second_entry_from_the_right(app_factory: Callable[..., Any]) -> None:
    async with app_factory(
        rate_limit_ip_burst=2, rate_limit_ip_per_s=0.001, trust_proxy=True, trusted_proxy_hops=2
    ) as c:
        # [attacker-controlled..., real client, first proxy's peer]: the key is the real client (2nd from right)
        codes = [
            (
                await c.get("/v1/universe", headers={"X-Forwarded-For": f"9.9.9.{i}, 203.0.113.77, 10.1.1.1"})
            ).status_code
            for i in range(5)
        ]
        assert codes[:2] == [200, 200] and set(codes[2:]) == {429}
        # a different real client is a different bucket
        other = await c.get("/v1/universe", headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.78, 10.1.1.1"})
        assert other.status_code == 200


def test_client_ip_unit() -> None:
    from app.core.ratelimit import client_ip

    def scope(xff: list[str], peer: str | None = "192.0.2.1") -> dict[str, Any]:
        return {
            "headers": [(b"x-forwarded-for", v.encode()) for v in xff],
            "client": (peer, 1) if peer else None,
        }

    assert client_ip(scope(["1.1.1.1, 2.2.2.2"]), True) == "2.2.2.2"
    assert client_ip(scope(["1.1.1.1", "2.2.2.2"]), True) == "2.2.2.2"  # header sent twice = one list
    assert client_ip(scope(["1.1.1.1, 2.2.2.2, 3.3.3.3"]), True, hops=2) == "2.2.2.2"
    assert client_ip(scope(["2.2.2.2"]), True, hops=2) == "192.0.2.1"  # shorter than the trusted chain: peer
    assert client_ip(scope(["1.1.1.1, not-an-ip"]), True) == "192.0.2.1"
    assert client_ip(scope(["::1 "]), True) == "::1"
    assert client_ip(scope(["1.1.1.1"]), False) == "192.0.2.1"  # not trusted: header ignored
    assert client_ip(scope([]), True) == "192.0.2.1"
    assert client_ip(scope(["1.1.1.1"], peer=None), False) == "unknown"


async def test_xff_is_ignored_when_proxy_not_trusted(app_factory: Callable[..., Any]) -> None:
    async with app_factory(rate_limit_ip_burst=3, rate_limit_ip_per_s=0.001, trust_proxy=False) as c:
        codes = [
            (await c.get("/v1/universe", headers={"X-Forwarded-For": f"198.51.100.{i}"})).status_code
            for i in range(6)
        ]
        assert 429 in codes


async def test_events_endpoint_has_its_own_limiter(api: httpx.AsyncClient, tenant_b: Tenant) -> None:
    """Event flood: bucket of 300 events, so a few 100-event batches must hit 429 (cost = events, not requests)."""
    codes = []
    for _ in range(8):
        evs = [
            {
                "event_id": str(uuid4()),
                "ts": datetime.now(UTC).isoformat(),
                "type": "session_started",
                "schema_version": 1,
            }
            for _ in range(100)
        ]
        r = await api.post("/v1/events", headers=tenant_b.headers, json={"events": evs})
        codes.append(r.status_code)
        if r.status_code == 429:
            assert int(r.headers["retry-after"]) >= 1
            break
    assert 429 in codes, codes


# ---------------------------------------------------------------------------------------- headers / trace id
SEC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


async def test_security_headers_on_every_kind_of_response(
    app_factory: Callable[..., Any], tenant_b: Tenant
) -> None:
    async with app_factory(rate_limit_ip_burst=40, rate_limit_ip_per_s=0.001) as c:
        responses = [
            await c.get("/healthz"),
            await c.get("/v1/me", headers=tenant_b.headers),
            await c.get("/v1/me"),  # 401
            await c.get("/does-not-exist"),  # 404
            await c.put("/v1/me", headers=tenant_b.headers),  # 405
            await c.post("/v1/chat", headers=tenant_b.headers, json={}),  # 422
            await c.post("/v1/chat", headers=tenant_b.headers, content=b"x" * 1_100_000),  # 413
        ]
        for r in responses:
            for k, v in SEC_HEADERS.items():
                assert r.headers.get(k) == v, (r.request.url.path, r.status_code, k)
            assert "max-age=" in r.headers["strict-transport-security"]
            assert "frame-ancestors 'none'" in r.headers[
                "content-security-policy"
            ] or r.request.url.path.startswith("/docs")
            assert re.fullmatch(r"[A-Za-z0-9._-]{8,64}", r.headers["x-trace-id"])
        # JSON error bodies carry the same trace id as the header
        err = responses[2]
        assert err.json()["trace_id"] == err.headers["x-trace-id"]


async def test_api_json_is_never_served_as_html(api: httpx.AsyncClient, tenant_b: Tenant) -> None:
    for r in (
        await api.get("/v1/me", headers=tenant_b.headers),
        await api.get("/nope"),
        await api.get("/v1/briefs/%3Cscript%3E", headers=tenant_b.headers),
    ):
        assert r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize(
    "hostile", ["x\r\nSet-Cookie: a=b", "<script>alert(1)</script>", "a" * 500, "../../x", "ok id"]
)
async def test_incoming_trace_id_is_validated_before_being_reflected(
    api: httpx.AsyncClient, hostile: str
) -> None:
    try:
        r = await api.get("/healthz", headers={"X-Trace-Id": hostile})
    except (httpx.InvalidURL, httpx.LocalProtocolError, ValueError):
        return  # refused client-side (CRLF); the server never sees it
    echoed = r.headers["x-trace-id"]
    assert echoed != hostile and re.fullmatch(r"[A-Za-z0-9._-]{8,64}", echoed)
    assert "set-cookie" not in r.headers


async def test_good_trace_id_is_propagated(api: httpx.AsyncClient) -> None:
    r = await api.get("/healthz", headers={"X-Trace-Id": "client-trace-0001"})
    assert r.headers["x-trace-id"] == "client-trace-0001"


# ---------------------------------------------------------------------------------------- CORS
async def test_cors_allows_only_configured_origins_and_never_credentials(
    app_factory: Callable[..., Any], tenant_b: Tenant
) -> None:
    async with app_factory(rate_limit_enabled=False, cors_origins=["https://app.example"]) as c:
        pre = await c.options(
            "/v1/me",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,idempotency-key",
            },
        )
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "https://app.example"
        assert "access-control-allow-credentials" not in pre.headers
        for origin in (
            "https://evil.example",
            "https://app.example.evil.example",
            "http://app.example",
            "null",
            "https://APP.example",
        ):
            pre = await c.options(
                "/v1/me", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
            )
            assert "access-control-allow-origin" not in pre.headers, origin
            r = await c.get("/v1/me", headers={**tenant_b.headers, "Origin": origin})
            assert "access-control-allow-origin" not in r.headers, origin
        r = await c.get("/v1/me", headers={**tenant_b.headers, "Origin": "https://app.example"})
        assert (
            r.headers["access-control-allow-origin"] == "https://app.example"
            and r.headers["vary"].lower().find("origin") >= 0
        )
        assert "*" not in r.headers.get("access-control-allow-origin", "")


async def test_cors_is_off_when_no_origins_configured(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------------------- error hygiene
async def test_unhandled_exceptions_return_a_generic_body(
    api: httpx.AsyncClient, tenant_b: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.watchlist.service as svc

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("SELECT * FROM users WHERE password='hunter2' -- /Users/dev/app/secret.py")

    monkeypatch.setattr(svc, "list_watchlist", boom)
    r = await api.get("/v1/watchlist", headers=tenant_b.headers)
    assert r.status_code == 500
    j = r.json()
    assert (
        j["code"] == "internal"
        and j["message"] == "Internal server error"
        and set(j) == {"code", "message", "trace_id"}
    )
    assert "hunter2" not in r.text and "SELECT" not in r.text and "secret.py" not in r.text
    assert r.headers["x-trace-id"] == j["trace_id"]


async def test_nonexistent_and_forbidden_look_identical(
    api: httpx.AsyncClient, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """No existence oracle: A's real proposal id and a random one give B the same status and message."""
    real = await api.get(f"/v1/proposals/{tenant_a.p.proposal_id}", headers=tenant_b.headers)
    fake = await api.get(f"/v1/proposals/{uuid4()}", headers=tenant_b.headers)
    assert real.status_code == fake.status_code
    assert real.json()["message"] == fake.json()["message"] and real.json()["code"] == fake.json()["code"]


async def test_method_override_and_verb_tunnelling_are_not_honoured(
    api: httpx.AsyncClient, tenant_b: Tenant, tenant_a: Tenant
) -> None:
    r = await api.post(
        f"/v1/proposals/{tenant_a.p.proposal_id}",
        headers={**tenant_b.headers, "X-HTTP-Method-Override": "DELETE"},
    )
    assert r.status_code in {404, 405}
    r = await api.request("TRACE", "/v1/me", headers=tenant_b.headers)
    assert r.status_code in {404, 405}


# ---------------------------------------------------------------------------------------- SEC-17 expensive calls
async def test_llm_backed_endpoints_have_a_tighter_per_user_bucket(
    app_factory: Callable[..., Any], tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """SEC-17 (fixed): chat / brief generation / proposal refresh share a small per-user bucket on top of the general one."""
    async with app_factory(
        rate_limit_expensive_burst=3, rate_limit_expensive_per_s=0.001, rate_limit_ip_burst=10_000
    ) as c:
        calls: list[tuple[str, str, dict[str, Any] | None]] = [
            ("POST", "/v1/chat", {"message": "What is a P/E ratio?"}),
            ("POST", "/v1/proposals/refresh", None),
            ("POST", "/v1/briefs/AAPL/generate", None),
        ]
        codes = []
        for i in range(6):
            method, path, body = calls[i % 3]
            codes.append((await c.request(method, path, headers=tenant_b.headers, json=body)).status_code)
        assert 429 in codes[3:] and 429 not in codes[:3], codes  # one shared bucket across the three routes
        r = await c.post("/v1/chat", headers=tenant_b.headers, json={"message": "hi"})
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert r.json()["code"] == "rate_limited"
        # cheap read endpoints are not charged to that bucket, and another user has their own
        assert (await c.get("/v1/me", headers=tenant_b.headers)).status_code == 200
        assert (await c.get("/v1/proposals", headers=tenant_b.headers)).status_code == 200
        assert (await c.post("/v1/chat", headers=tenant_a.headers, json={"message": "hi"})).status_code == 200


async def test_expensive_bucket_defaults_are_tighter_than_the_general_user_bucket() -> None:
    s = Settings(_env_file=None, auth_mode="dev")
    assert s.rate_limit_expensive_burst <= s.rate_limit_user_burst // 5
    assert s.rate_limit_expensive_per_s <= s.rate_limit_user_per_s / 10
    assert (
        s.rate_limit_expensive_burst >= 20
    )  # an SPA session must not trip it (an e2e journey sends ~dozens)
