"""Stream tokens (single use, <=60 s, hashed at rest) and DELETE /v1/account cascade."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.core.auth import consume_stream_token, hash_stream_token
from app.core.clock import FakeClock
from tests.db.rls_specs import RLS_TABLES, TABLE_SPECS, create_prereq

Conn = Any


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_stream_token_requires_auth(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post("/v1/stream/token")
    assert resp.status_code == 401


async def test_stream_token_is_single_use_hashed_and_short_lived(
    app_client: httpx.AsyncClient,
    make_user: Callable[..., Any],
    auth_headers: Callable[..., Any],
    db_admin: Conn,
) -> None:
    uid = await make_user()
    resp = await app_client.post("/v1/stream/token", headers=auth_headers(uid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == 60 and len(body["token"]) >= 40

    row = await db_admin.fetchrow("select * from stream_tokens where user_id = $1", uid)
    assert row["token_hash"] == hash_stream_token(body["token"])
    assert row["token_hash"] != body["token"]  # only the hash is stored
    assert (row["expires_at"] - row["created_at"]).total_seconds() <= 60
    assert body["token"] not in str(dict(row))

    now = datetime.now(UTC)
    assert await consume_stream_token(body["token"], now) == uid
    assert await consume_stream_token(body["token"], now) is None  # burned
    assert await consume_stream_token("bogus-token", now) is None
    assert await consume_stream_token("", now) is None


async def test_stream_token_expires_after_60_seconds(
    app_factory: Callable[..., Any], make_user: Callable[..., Any], auth_headers: Callable[..., Any]
) -> None:
    uid = await make_user()
    clock = FakeClock(start=datetime.now(UTC))
    async with app_factory(clock=clock) as client:
        fresh = (await client.post("/v1/stream/token", headers=auth_headers(uid))).json()["token"]
        stale = (await client.post("/v1/stream/token", headers=auth_headers(uid))).json()["token"]
        clock.advance(30)
        assert await consume_stream_token(fresh, clock.now()) == uid
        clock.advance(31)  # 61 s after minting
        assert await consume_stream_token(stale, clock.now()) is None


async def test_concurrent_consumption_burns_exactly_once(
    app_client: httpx.AsyncClient, make_user: Callable[..., Any], auth_headers: Callable[..., Any]
) -> None:
    import asyncio

    uid = await make_user()
    token = (await app_client.post("/v1/stream/token", headers=auth_headers(uid))).json()["token"]
    now = datetime.now(UTC)
    results = await asyncio.gather(*(consume_stream_token(token, now) for _ in range(8)))
    assert [r for r in results if r is not None] == [uid]


async def test_stream_tokens_are_per_user(
    app_client: httpx.AsyncClient, make_user: Callable[..., Any], auth_headers: Callable[..., Any]
) -> None:
    a, b = await make_user(), await make_user()
    ta = (await app_client.post("/v1/stream/token", headers=auth_headers(a))).json()["token"]
    tb = (await app_client.post("/v1/stream/token", headers=auth_headers(b))).json()["token"]
    now = datetime.now(UTC)
    assert await consume_stream_token(tb, now) == b
    assert await consume_stream_token(ta, now) == a


async def test_delete_account_validation_and_auth(
    app_client: httpx.AsyncClient,
    make_user: Callable[..., Any],
    auth_headers: Callable[..., Any],
    db_admin: Conn,
) -> None:
    uid = await make_user()
    assert (await app_client.request("DELETE", "/v1/account", json={"confirm": "DELETE"})).status_code == 401
    for bad in ({}, {"confirm": "delete"}, {"confirm": "yes"}):
        r = await app_client.request("DELETE", "/v1/account", json=bad, headers=auth_headers(uid))
        assert r.status_code == 422
    assert await db_admin.fetchval("select count(*) from users where id = $1", uid) == 1


async def test_delete_account_cascades_everything_and_anonymises_audit(
    app_client: httpx.AsyncClient,
    auth_headers: Callable[..., Any],
    db_admin: Conn,
    db_as_user: Callable[..., Any],
) -> None:
    owner = await create_prereq(db_admin)
    other = await create_prereq(db_admin)
    for who in (owner, other):
        async with db_as_user(who.user_id) as conn:
            for spec in TABLE_SPECS.values():
                if spec.insert is not None:
                    await spec.insert(conn, who)

    resp = await app_client.request(
        "DELETE", "/v1/account", json={"confirm": "DELETE"}, headers=auth_headers(owner.user_id)
    )
    assert resp.status_code == 204

    async def total(uid: UUID) -> dict[str, int]:
        out = {}
        for t in RLS_TABLES:
            col = TABLE_SPECS[t].owner_col
            n = int(await db_admin.fetchval(f"select count(*) from {t} where {col} = $1", uid))
            if n:
                out[t] = n
        return out

    assert await total(owner.user_id) == {}  # every row for the user is gone, embeddings included
    assert (await total(other.user_id)).keys() == set(RLS_TABLES)  # the other tenant is untouched
    # audit trail survives, anonymised, including the deletion record
    deleted = await db_admin.fetch(
        "select user_id, details from audit_log where action = 'account.delete' order by id desc limit 1"
    )
    assert deleted and deleted[0]["user_id"] is None and deleted[0]["details"]["anonymised"] is True
    assert await db_admin.fetchval("select count(*) from audit_log where user_id = $1", owner.user_id) == 0
    await db_admin.execute("delete from users where id = $1", other.user_id)


async def test_delete_account_calls_supabase_admin_delete(
    app_client: httpx.AsyncClient,
    make_user: Callable[..., Any],
    auth_headers: Callable[..., Any],
    db_admin: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-3: DELETE /account also removes the Supabase auth user (id passed through, settings injected)."""
    from app.modules.profile import supabase_admin

    seen: list[UUID] = []

    async def fake_delete(_settings: Any, user_id: UUID, client: Any = None) -> bool:
        seen.append(user_id)
        return True

    monkeypatch.setattr(supabase_admin, "delete_auth_user", fake_delete)
    uid = await make_user()
    resp = await app_client.request(
        "DELETE", "/v1/account", json={"confirm": "DELETE"}, headers=auth_headers(uid)
    )
    assert resp.status_code == 204
    assert seen == [uid]
    assert await db_admin.fetchval("select count(*) from users where id = $1", uid) == 0


async def test_delete_account_rolls_back_when_supabase_fails(
    app_client: httpx.AsyncClient,
    make_user: Callable[..., Any],
    auth_headers: Callable[..., Any],
    db_admin: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the auth provider cannot delete the user the local delete is rolled back (503) so a retry is safe."""
    from app.modules.profile import supabase_admin

    async def boom(_settings: Any, user_id: UUID, client: Any = None) -> bool:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(supabase_admin, "delete_auth_user", boom)
    uid = await make_user()
    resp = await app_client.request(
        "DELETE", "/v1/account", json={"confirm": "DELETE"}, headers=auth_headers(uid)
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"
    assert await db_admin.fetchval("select count(*) from users where id = $1", uid) == 1
