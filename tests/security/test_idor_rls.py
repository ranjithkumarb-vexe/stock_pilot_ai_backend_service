"""SEC-01 area 1: authorisation. User B vs user A's ids on every id-taking endpoint, list leakage, stream tokens,
and RLS reached through the API (T-SEC-03)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from app.core.auth import consume_stream_token, hash_stream_token
from app.core.clock import FakeClock
from tests.security.conftest import MARK_CHAT, MARK_EMAIL_LOCAL, MARK_MEMORY, Tenant

pytestmark = pytest.mark.db

NOT_OURS = {403, 404}


# --------------------------------------------------------------------------------------------- direct id access
async def test_b_cannot_read_or_change_a_proposal(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    pid = tenant_a.p.proposal_id
    before = await db_admin.fetchrow("select status, decided_at from proposals where id = $1", pid)
    r = await api.get(f"/v1/proposals/{pid}", headers=tenant_b.headers)
    assert r.status_code in NOT_OURS, r.text
    for decision in (
        {"decision": "approve"},
        {"decision": "reject", "reason": "disagree"},
        {"decision": "modify", "qty": 1},
    ):
        r = await api.post(f"/v1/proposals/{pid}/decision", headers=tenant_b.headers, json=decision)
        assert r.status_code in NOT_OURS, (decision, r.status_code, r.text)
    assert dict(before) == dict(
        await db_admin.fetchrow("select status, decided_at from proposals where id = $1", pid)
    )


async def test_b_cannot_cancel_or_see_a_order(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    oid = tenant_a.p.order_id
    before = await db_admin.fetchval("select status from orders where id = $1", oid)
    r = await api.delete(f"/v1/orders/{oid}", headers=tenant_b.headers)
    assert r.status_code in NOT_OURS, r.text
    assert await db_admin.fetchval("select status from orders where id = $1", oid) == before
    listing = await api.get("/v1/orders?limit=200", headers=tenant_b.headers)
    assert listing.status_code == 200 and str(oid) not in listing.text


async def test_b_cannot_act_on_a_alert(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    aid = tenant_a.alert_id
    before = await db_admin.fetchrow("select state, snoozed_until from alerts where id = $1", aid)
    for action in ("open", "dismiss", "snooze", "restore"):
        r = await api.post(f"/v1/alerts/{aid}/action", headers=tenant_b.headers, json={"action": action})
        assert r.status_code in NOT_OURS, (action, r.status_code, r.text)
    after = await db_admin.fetchrow("select state, snoozed_until from alerts where id = $1", aid)
    assert dict(before) == dict(after)
    assert str(aid) not in (await api.get("/v1/alerts?limit=200", headers=tenant_b.headers)).text


async def test_b_cannot_read_a_citation(api: httpx.AsyncClient, tenant_a: Tenant, tenant_b: Tenant) -> None:
    r = await api.get(f"/v1/citations/{tenant_a.citation_id}", headers=tenant_b.headers)
    assert r.status_code in NOT_OURS, r.text


async def test_b_cannot_read_a_trace_by_uuid_or_by_request_trace_id(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    ref = tenant_a.p.trace_ref
    request_trace_id = await db_admin.fetchval("select trace_id from traces where id = $1", ref)
    for ident in (str(ref), request_trace_id):
        assert (await api.get(f"/v1/audit/traces/{ident}", headers=tenant_a.headers)).status_code == 200
        r = await api.get(f"/v1/audit/traces/{ident}", headers=tenant_b.headers)
        assert r.status_code in NOT_OURS, (ident, r.text)
    listing = await api.get("/v1/audit/traces?limit=200", headers=tenant_b.headers)
    assert str(ref) not in listing.text and request_trace_id not in listing.text


async def test_b_cannot_delete_a_memory(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    r = await api.delete(f"/v1/profile/memories/{tenant_a.memory_id}", headers=tenant_b.headers)
    assert r.status_code in NOT_OURS | {204}, r.text  # 204 is tolerable only if nothing was deleted
    assert await db_admin.fetchval("select count(*) from memories where id = $1", tenant_a.memory_id) == 1


async def test_b_cannot_read_or_write_a_chat_thread(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    tid = tenant_a.p.thread_id
    r = await api.get(f"/v1/chat/history?thread_id={tid}", headers=tenant_b.headers)
    assert r.status_code in NOT_OURS and MARK_CHAT not in r.text
    n = await db_admin.fetchval("select count(*) from chat_messages where thread_id = $1", tid)
    r = await api.post(
        "/v1/chat", headers=tenant_b.headers, json={"message": "What is a P/E ratio?", "thread_id": str(tid)}
    )
    assert r.status_code in NOT_OURS, r.text
    assert await db_admin.fetchval("select count(*) from chat_messages where thread_id = $1", tid) == n
    # B's own default history never falls back to A's thread
    r = await api.get("/v1/chat/history", headers=tenant_b.headers)
    assert r.status_code == 200 and MARK_CHAT not in r.text


async def test_why_endpoint_is_scoped(api: httpx.AsyncClient, tenant_a: Tenant, tenant_b: Tenant) -> None:
    pairs = [
        ("alert", tenant_a.alert_id),
        ("proposal", tenant_a.p.proposal_id),
        ("brief", tenant_a.p.brief_id),
    ]
    for typ, oid in pairs:
        r = await api.get(f"/v1/why/{typ}/{oid}", headers=tenant_b.headers)
        assert r.status_code in NOT_OURS | {422}, (typ, r.status_code, r.text)


async def test_order_cannot_reference_another_users_proposal(
    api: httpx.AsyncClient, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    body = {
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": 1,
        "proposal_id": str(tenant_a.p.proposal_id),
    }
    r = await api.post("/v1/orders", headers={**tenant_b.headers, "Idempotency-Key": str(uuid4())}, json=body)
    assert r.status_code in NOT_OURS | {409, 422}, r.text
    assert r.status_code != 201
    r = await api.post("/v1/orders/preview", headers=tenant_b.headers, json=body)
    assert r.status_code != 200 or "proposal" not in json.dumps(r.json()).lower().replace("proposal_id", "")


async def test_events_cannot_poison_or_read_other_users_entities(
    api: httpx.AsyncClient, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    ev = {
        "event_id": str(uuid4()),
        "ts": datetime.now(UTC).isoformat(),
        "type": "alert_dismissed",
        "entity": {"alert_id": str(tenant_a.alert_id)},
        "schema_version": 1,
    }
    before = await db_admin.fetchrow(
        "select state, snoozed_until from alerts where id = $1", tenant_a.alert_id
    )
    r = await api.post("/v1/events", headers=tenant_b.headers, json={"events": [ev]})
    assert r.status_code in {200, 202, 422}
    assert (
        await db_admin.fetchrow("select state, snoozed_until from alerts where id = $1", tenant_a.alert_id)
    ) == before
    # whatever was stored is stored under B, never A
    assert (
        await db_admin.fetchval(
            "select count(*) from events where event_id = $1 and user_id = $2",
            UUID(str(ev["event_id"])),
            tenant_a.uid,
        )
        == 0
    )


async def test_server_only_event_types_cannot_be_forged(
    api: httpx.AsyncClient, db_admin: Any, tenant_b: Tenant
) -> None:
    eid = uuid4()
    for typ in ("order_filled", "proposal_approved", "rule_override"):
        ev = {"event_id": str(eid), "ts": datetime.now(UTC).isoformat(), "type": typ, "schema_version": 1}
        r = await api.post("/v1/events", headers=tenant_b.headers, json={"events": [ev]})
        assert r.status_code in {200, 202, 422}
        assert (
            await db_admin.fetchval("select count(*) from events where event_id = $1 and type = $2", eid, typ)
            == 0
        )


# ---------------------------------------------------------------------------------------------- list leakage
LIST_ENDPOINTS = [
    "/v1/me",
    "/v1/profile",
    "/v1/profile/export",
    "/v1/portfolio",
    "/v1/portfolio/performance",
    "/v1/portfolio/risk",
    "/v1/proposals",
    "/v1/orders?limit=200",
    "/v1/alerts?limit=200",
    "/v1/feed",
    "/v1/watchlist",
    "/v1/chat/history",
    "/v1/audit/traces?limit=200",
]


@pytest.mark.parametrize("path", LIST_ENDPOINTS)
async def test_b_never_sees_a_data_in_any_list_or_export(
    api: httpx.AsyncClient, tenant_a: Tenant, tenant_b: Tenant, path: str
) -> None:
    r = await api.get(path, headers=tenant_b.headers)
    assert r.status_code in {200, 404}, (path, r.status_code, r.text[:200])
    needles = [
        str(tenant_a.uid),
        tenant_a.email,
        MARK_EMAIL_LOCAL,
        MARK_CHAT,
        MARK_MEMORY,
        str(tenant_a.alert_id),
        str(tenant_a.p.proposal_id),
        str(tenant_a.p.order_id),
        str(tenant_a.p.portfolio_id),
        str(tenant_a.p.trace_ref),
    ]
    for n in needles:
        assert n not in r.text, f"{path} leaked {n!r}"


async def test_a_can_see_own_data_control(api: httpx.AsyncClient, tenant_a: Tenant) -> None:
    """Control: the leakage assertions above are meaningful because A really sees these rows."""
    r = await api.get("/v1/profile/export", headers=tenant_a.headers)
    assert r.status_code == 200
    assert MARK_MEMORY in r.text and MARK_CHAT in r.text and tenant_a.email in r.text
    assert str(tenant_a.alert_id) in (await api.get("/v1/alerts?limit=200", headers=tenant_a.headers)).text


# ---------------------------------------------------------------------------------------------- RLS via the API
async def test_rls_holds_under_a_hostile_session_context(
    db_as_user: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """The API role cannot be tricked by app.user_id games: unset, empty and B's id all see nothing of A."""
    tables = ("proposals", "orders", "alerts", "memories", "chat_messages", "traces", "briefs", "profiles")
    for ctx in (None, "", str(tenant_b.uid)):
        async with db_as_user(ctx) as conn:
            for table in tables:
                n = await conn.fetchval(f"select count(*) from {table} where user_id = $1", tenant_a.uid)
                assert n == 0, f"{table} visible with app.user_id={ctx!r}"
    with pytest.raises(asyncpg.PostgresError):  # junk context: the policy cast errors => no access either
        async with db_as_user("not-a-uuid") as conn:
            await conn.fetchval("select count(*) from proposals")


async def test_app_role_has_no_bypass_or_superuser(db_admin: Any) -> None:
    row = await db_admin.fetchrow(
        "select rolsuper, rolbypassrls, rolcreatedb, rolcreaterole from pg_roles where rolname = 'stockpilot_app'"
    )
    assert not any(row.values()), dict(row)


async def test_every_user_table_has_forced_rls(db_admin: Any) -> None:
    rows = await db_admin.fetch(
        """
        select c.relname, c.relrowsecurity, c.relforcerowsecurity
          from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r'
           and exists (select 1 from pg_attribute a where a.attrelid = c.oid and a.attname = 'user_id' and not a.attisdropped)
        """
    )
    assert rows
    assert [r["relname"] for r in rows if not (r["relrowsecurity"] and r["relforcerowsecurity"])] == []


# ---------------------------------------------------------------------------------------------- stream token
async def test_stream_token_is_single_use_short_lived_and_hashed(
    app_factory: Any, db_admin: Any, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    from datetime import UTC, datetime, timedelta

    clock = FakeClock(start=datetime.now(UTC))
    async with app_factory(clock=clock, rate_limit_enabled=False) as api:
        r = await api.post("/v1/stream/token", headers=tenant_a.headers)
        assert r.status_code == 200
        token, ttl = r.json()["token"], r.json()["expires_in"]
        assert ttl <= 60 and len(token) >= 40
        # only the hash is stored
        assert await db_admin.fetchval("select count(*) from stream_tokens where token_hash = $1", token) == 0
        assert (
            await db_admin.fetchval(
                "select user_id from stream_tokens where token_hash = $1", hash_stream_token(token)
            )
            == tenant_a.uid
        )
        # a stream token is not a bearer credential, and a bearer is not a stream token
        assert (await api.get("/v1/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
        bearer = tenant_a.headers["Authorization"].split()[1]
        assert (await api.get(f"/v1/stream?token={bearer}")).status_code == 401
        # B cannot mint a token that resolves to A, and tokens are bound to their minting user
        rb = await api.post("/v1/stream/token", headers=tenant_b.headers)
        assert await consume_stream_token(rb.json()["token"], clock.now()) == tenant_b.uid
        # single use
        assert await consume_stream_token(token, clock.now()) == tenant_a.uid
        assert await consume_stream_token(token, clock.now()) is None
        r = await api.get(f"/v1/stream?token={token}")
        assert r.status_code == 401
        # expiry
        r2 = await api.post("/v1/stream/token", headers=tenant_a.headers)
        later = clock.now() + timedelta(seconds=61)
        assert await consume_stream_token(r2.json()["token"], later) is None


@pytest.mark.parametrize("token", ["", "x", "a" * 129, "' OR '1'='1", "../../etc/passwd", "%00", "null"])
async def test_stream_rejects_bad_tokens(api: httpx.AsyncClient, token: str) -> None:
    r = await api.get("/v1/stream", params={"token": token})
    assert r.status_code == 401, r.text


async def test_stream_tokens_not_readable_by_other_tenants(
    db_as_user: Any, tenant_a: Tenant, tenant_b: Tenant, api: httpx.AsyncClient
) -> None:
    await api.post("/v1/stream/token", headers=tenant_a.headers)
    async with db_as_user(tenant_b.uid) as conn:
        assert await conn.fetchval("select count(*) from stream_tokens") == 0
