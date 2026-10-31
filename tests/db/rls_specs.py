"""Row factories + the isolation probe shared by the RLS, mutation and cascade tests.

HOW TO ADD A NEW USER TABLE (streams A/B/C): add one entry to ``TABLE_SPECS`` that inserts a valid row owned
by ``p.user_id`` (use ``p.*`` for parent ids). ``tests/db/test_rls.py`` fails until you do, and then runs the
full cross-tenant probe on your table automatically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from app.core.db import vec_literal

Conn = Any
AsUser = Callable[[UUID | None], AbstractAsyncContextManager[Conn]]

REFERENCE_TABLES = frozenset(
    {
        "tickers",
        "xbrl_facts",
        "raw_documents",
        "doc_chunks",
        "prices_daily",
        "corporate_actions",
        "eval_runs",
    }
)
SYSTEM_TABLES = frozenset({"schema_migrations"})


@dataclass(frozen=True)
class Prereq:
    """Parent rows owned by ``user_id`` (created by the owner role, outside RLS)."""

    user_id: UUID
    portfolio_id: UUID
    proposal_id: UUID
    order_id: UUID
    brief_id: UUID
    thread_id: UUID
    trace_ref: UUID


async def create_prereq(admin: Conn, user_id: UUID | None = None) -> Prereq:
    uid = user_id or uuid4()
    await admin.execute(
        "insert into tickers (symbol, name, sector) values ('AAPL', 'Apple', 'Technology') on conflict do nothing"
    )
    await admin.execute("insert into users (id, email) values ($1, $2)", uid, f"{uid.hex[:8]}@rls.test")
    pf = await admin.fetchval(
        "insert into portfolios (user_id, name, starting_cash) values ($1, 'Main', 100000) returning id",
        uid,
    )
    prop = await admin.fetchval(
        "insert into proposals (user_id, portfolio_id, type, symbol) values ($1, $2, 'trade', 'AAPL') returning id",
        uid,
        pf,
    )
    order = await admin.fetchval(
        "insert into orders (portfolio_id, user_id, symbol, side, type, qty, idempotency_key) "
        "values ($1, $2, 'AAPL', 'buy', 'market', 1, $3) returning id",
        pf,
        uid,
        uuid4().hex,
    )
    brief = await admin.fetchval(
        "insert into briefs (user_id, symbol, as_of, style_hash) values ($1, 'AAPL', current_date, $2) returning id",
        uid,
        uuid4().hex,
    )
    thread = await admin.fetchval(
        "insert into chat_threads (user_id, title) values ($1, 't') returning id", uid
    )
    trace = await admin.fetchval(
        "insert into traces (trace_id, user_id, name) values ($1, $2, 'brief') returning id",
        uuid4().hex,
        uid,
    )
    return Prereq(uid, pf, prop, order, brief, thread, trace)


Insert = Callable[[Conn, Prereq], Awaitable[Any]]


@dataclass(frozen=True)
class Spec:
    owner_col: str
    insert: Insert | None  # None => the row already exists via Prereq (users)


async def _ins(conn: Conn, sql: str, *args: Any) -> Any:
    return await conn.execute(sql, *args)


def _embedding() -> str:
    return vec_literal([0.01] * 1024)


TABLE_SPECS: dict[str, Spec] = {
    "users": Spec("id", None),
    "profiles": Spec("user_id", lambda c, p: _ins(c, "insert into profiles (user_id, risk_tier_stated) values ($1, 3)", p.user_id)),
    "risk_rules": Spec("user_id", lambda c, p: _ins(c, "insert into risk_rules (user_id, rule_key, value) values ($1, 'pos', 10)", p.user_id)),
    "watchlist_items": Spec("user_id", lambda c, p: _ins(c, "insert into watchlist_items (user_id, symbol) values ($1, 'AAPL')", p.user_id)),
    "sector_prefs": Spec("user_id", lambda c, p: _ins(c, "insert into sector_prefs (user_id, sector, pref) values ($1, 'Energy', -1)", p.user_id)),
    "portfolios": Spec("user_id", lambda c, p: _ins(c, "insert into portfolios (user_id, name, starting_cash) values ($1, $2, 5)", p.user_id, uuid4().hex)),
    "proposals": Spec("user_id", lambda c, p: _ins(c, "insert into proposals (user_id, portfolio_id, type) values ($1, $2, 'review')", p.user_id, p.portfolio_id)),
    "orders": Spec("user_id", lambda c, p: _ins(c, "insert into orders (portfolio_id, user_id, symbol, side, type, qty, idempotency_key) values ($1, $2, 'AAPL', 'sell', 'market', 2, $3)", p.portfolio_id, p.user_id, uuid4().hex)),
    "fills": Spec("user_id", lambda c, p: _ins(c, "insert into fills (order_id, user_id, portfolio_id, symbol, qty, price) values ($1, $2, $3, 'AAPL', 1, 100)", p.order_id, p.user_id, p.portfolio_id)),
    "cash_ledger": Spec("user_id", lambda c, p: _ins(c, "insert into cash_ledger (portfolio_id, user_id, kind, amount) values ($1, $2, 'deposit', 10)", p.portfolio_id, p.user_id)),
    "positions": Spec("user_id", lambda c, p: _ins(c, "insert into positions (portfolio_id, symbol, user_id, qty, avg_cost) values ($1, 'AAPL', $2, 1, 100)", p.portfolio_id, p.user_id)),
    "equity_snapshots": Spec("user_id", lambda c, p: _ins(c, "insert into equity_snapshots (portfolio_id, user_id, ts, equity, cash) values ($1, $2, now(), 100, 50)", p.portfolio_id, p.user_id)),
    "briefs": Spec("user_id", lambda c, p: _ins(c, "insert into briefs (user_id, symbol, as_of, style_hash) values ($1, 'AAPL', current_date, $2)", p.user_id, uuid4().hex)),
    "citations": Spec("user_id", lambda c, p: _ins(c, "insert into citations (brief_id, user_id, kind, ref_id) values ($1, $2, 'xbrl', '1')", p.brief_id, p.user_id)),
    "alerts": Spec("user_id", lambda c, p: _ins(c, "insert into alerts (user_id, symbol, type, dedupe_key) values ($1, 'AAPL', 'filing', $2)", p.user_id, uuid4().hex)),
    "chat_threads": Spec("user_id", lambda c, p: _ins(c, "insert into chat_threads (user_id, title) values ($1, 'x')", p.user_id)),
    "chat_messages": Spec("user_id", lambda c, p: _ins(c, "insert into chat_messages (thread_id, user_id, role, content) values ($1, $2, 'user', 'hi')", p.thread_id, p.user_id)),
    "events": Spec("user_id", lambda c, p: _ins(c, "insert into events (event_id, user_id, ts, type) values ($1, $2, now(), 'brief_open')", uuid4(), p.user_id)),
    "user_features": Spec("user_id", lambda c, p: _ins(c, "insert into user_features (user_id, key, value) values ($1, 'style', '{}'::jsonb)", p.user_id)),
    "user_interest_vector": Spec("user_id", lambda c, p: _ins(c, "insert into user_interest_vector (user_id, dim, weight) values ($1, 'sector:tech', 0.5)", p.user_id)),
    "bandit_arms": Spec("user_id", lambda c, p: _ins(c, "insert into bandit_arms (user_id, arm, context) values ($1, 'breach', 'ctx')", p.user_id)),
    "memories": Spec("user_id", lambda c, p: _ins(c, "insert into memories (user_id, kind, text, embedding) values ($1, 'semantic', 'prefers dividends', $2::vector)", p.user_id, _embedding())),
    "personalization_audit": Spec("user_id", lambda c, p: _ins(c, "insert into personalization_audit (user_id, output_type, output_id) values ($1, 'feed', 'x')", p.user_id)),
    "llm_calls": Spec("user_id", lambda c, p: _ins(c, "insert into llm_calls (user_id, model, mode, data_class) values ($1, 'm', 'standard', 'user')", p.user_id)),
    "audit_log": Spec("user_id", lambda c, p: _ins(c, "insert into audit_log (user_id, action, entity) values ($1, 'x', 'y')", p.user_id)),
    "traces": Spec("user_id", lambda c, p: _ins(c, "insert into traces (trace_id, user_id, name) values ($1, $2, 'chat')", uuid4().hex, p.user_id)),
    "trace_steps": Spec("user_id", lambda c, p: _ins(c, "insert into trace_steps (trace_ref, user_id, idx, name) values ($1, $2, 0, 's')", p.trace_ref, p.user_id)),
    "stream_tokens": Spec("user_id", lambda c, p: _ins(c, "insert into stream_tokens (token_hash, user_id, expires_at) values ($1, $2, now() + interval '1 minute')", uuid4().hex, p.user_id)),
}  # fmt: skip

RLS_TABLES = sorted(TABLE_SPECS)


async def attempt(conn: Conn, sql: str, *args: Any) -> str:
    """Run one statement in a savepoint; return its command tag or ``ERR:<sqlstate>``."""
    try:
        async with conn.transaction():
            return str(await conn.execute(sql, *args))
    except asyncpg.PostgresError as exc:
        return f"ERR:{exc.sqlstate}"


async def attempt_insert(conn: Conn, spec: Spec, p: Prereq) -> str:
    assert spec.insert is not None
    try:
        async with conn.transaction():
            await spec.insert(conn, p)
            return "INSERT"
    except asyncpg.PostgresError as exc:
        return f"ERR:{exc.sqlstate}"


async def count(conn: Conn, table: str, where: str, *args: Any) -> int:
    return int(await conn.fetchval(f"select count(*) from {table} where {where}", *args))


def _blocked(result: str) -> bool:
    """Blocked = RLS violation / no privilege (42501) or zero rows touched."""
    return result in {"UPDATE 0", "DELETE 0"} or result == "ERR:42501"


async def probe_table(table: str, admin: Conn, as_user: AsUser) -> list[str]:
    """Cross-tenant probe for one table. Returns a list of failures (empty == isolated).

    Positive controls matter: a policy that was *dropped* (default deny) must fail too, because the owner
    could no longer read/write their own rows.
    """
    spec = TABLE_SPECS[table]
    col = spec.owner_col
    a = await create_prereq(admin)
    b = await create_prereq(admin)
    failures: list[str] = []

    for owner in (a, b):
        if spec.insert is not None:
            async with as_user(owner.user_id) as conn:
                res = await attempt_insert(conn, spec, owner)
                if res != "INSERT":
                    failures.append(f"owner could not insert own row ({res})")

    async with as_user(a.user_id) as conn:
        own = await count(conn, table, f"{col} = $1", a.user_id)
        if own < 1:
            failures.append("owner cannot see own rows")
        if await count(conn, table, f"{col} = $1", b.user_id) != 0:
            failures.append("user A sees user B rows")

    async with as_user(b.user_id) as conn:
        if await count(conn, table, f"{col} = $1", a.user_id) != 0:
            failures.append("LEAK: user B sees user A rows")
        total = await count(conn, table, "true")
        mine = await count(conn, table, f"{col} = $1", b.user_id)
        if total != mine:
            failures.append(f"LEAK: B sees {total - mine} foreign rows in unfiltered select")
        upd = await attempt(conn, f"update {table} set {col} = {col} where {col} = $1", a.user_id)
        if not _blocked(upd):
            failures.append(f"LEAK: B updated A rows ({upd})")
        dele = await attempt(conn, f"delete from {table} where {col} = $1", a.user_id)
        if not _blocked(dele):
            failures.append(f"LEAK: B deleted A rows ({dele})")
        rehome = await attempt(conn, f"update {table} set {col} = $1 where {col} = $2", a.user_id, b.user_id)
        if not _blocked(rehome):
            failures.append(f"LEAK: B re-homed own row to A ({rehome})")
        if spec.insert is not None:
            res = await attempt_insert(conn, spec, a)
            if res != "ERR:42501":
                failures.append(f"LEAK: B inserted a row owned by A ({res})")

    async with as_user(None) as conn:
        if await count(conn, table, "true") != 0:
            failures.append("LEAK: rows visible with no app.user_id")
        if spec.insert is not None:
            res = await attempt_insert(conn, spec, a)
            if res != "ERR:42501":
                failures.append(f"LEAK: insert allowed with no app.user_id ({res})")

    async with as_user(a.user_id) as conn:
        if await count(conn, table, f"{col} = $1", a.user_id) != own:
            failures.append("A's rows changed after B's attempts")

    await admin.execute("delete from users where id = any($1::uuid[])", [a.user_id, b.user_id])
    return failures
