# Handoff notes (newest first)

## Open change requests
CR-1 | from: backend-engineer (foundation) | to: architect | `docs/workflow/CONTRACT.md` error codes: the foundation maps HTTP 405 -> `method_not_allowed` and HTTP 413 (body cap) -> `payload_too_large`, which are not in the contract list. Please add them (or tell me to fold them into `validation_error`); FE maps unknown codes to a generic message meanwhile | FE `src/api/errors.ts` | resolved 2026-10-31 (CONTRACT.md updated)
CR-2 | from: backend-engineer (foundation) | to: architect | CONTRACT §Auth: `DELETE /account` returns **204 No Content** (no body) and dev-login returns exactly `{access_token, expires_in, user}` where `user` has the `/me` shape; note `rate_limited` also carries `details.retry_after_s` | FE api client | resolved 2026-10-31 (CONTRACT.md updated)
CR-3 | from: backend-engineer (foundation) | to: stream A (BE-05) | `DELETE /v1/account` is a skeleton: it deletes the `users` row (cascade) and anonymises audit rows. BE-05 must add: JSON export hook, deletion of the Supabase auth user (needs `SUPABASE_SERVICE_ROLE_KEY`; until then an old JWT stays valid until expiry and the next request re-creates an *empty* `users` row), onboarding/profile specifics | BE-05 | open
CR-4 | from: backend-engineer (foundation) | to: database-engineer (BE-04) | `scripts/seed.py` is a stub that prints "not implemented"; replace it. Persona ids/emails/names live in `app/core/personas.py` (priya/alex/sam, `uuid5`-deterministic) - seed must reuse them (`insert ... on conflict do nothing`; dev-login already creates the `users` row with `onboarded_at = now()` for personas) | BE-04 | open

Template: `CR-<n> | from | to | what/why | blocking task ids | status`

## 2026-10-31 - backend-engineer (foundation): BE-01, BE-02, BE-03 done
**How to run** (macOS, Python 3.12 + uv, local Postgres 15 with pgvector; no Docker/Redis needed)
```
make setup     # uv sync; creates dev DB + $SP_TEST_DB; role stockpilot_app; applies migrations (idempotent)
make test      # 228 tests, ~7 s, coverage 92%
make lint      # ruff check + ruff format --check + mypy --strict
make run       # PORT=8000 by default; streams: make run PORT=8001 SP_TEST_DB=stockpilot_test_a
make openapi   # docs/contracts/openapi.json (no DB needed)
```
`.env` is git-ignored (mine uses unix-socket DSNs `...@/stockpilot_dev?host=/tmp`); `.env.example` documents every variable. Env vars always beat `.env`.
**Per-stream isolation:** set `SP_TEST_DB=stockpilot_test_a|b|c` (the conftest creates + wipes + migrates it at session start; the name must contain `test`/`e2e`). The migration tests use `${SP_TEST_DB}_mig`. **Never `pkill uvicorn`** - other streams run servers on 8001-8003; kill by `lsof -ti tcp:<port>`.

**Fixtures you can rely on** (`tests/conftest.py`, session-scoped event loop - `asyncio_default_*_loop_scope = "session"`)
- `db_admin` - asyncpg connection as owner (RLS bypassed) to the migrated test DB; jsonb codec installed.
- `db_as_user(uid)` - `async with db_as_user(uid) as conn:` real `stockpilot_app` connection, one transaction, `app.user_id` set, **commits on clean exit** (so two blocks can see each other's rows). `db_as_user(None)` = no user context. After a DB error inside a block the tx is aborted: wrap expected failures in `async with conn.transaction():` (savepoint).
- `app_client` - `httpx.AsyncClient` on the real app (lifespan running, real pool, test DB, `SystemClock`). `app_factory(clock=FakeClock(start=datetime.now(UTC)), rate_limit_ip_burst=3, auth_mode="supabase", ...)` builds one with overrides (use a *real-now* FakeClock start, otherwise dev JWTs minted from the fake clock are already expired).
- `make_user(email=None, display_name=None)` -> UUID (row cleaned up at teardown by cascade). `auth_headers(uid, email=None, ttl_s=None)` -> `{"Authorization": "Bearer ..."}`. `fake_clock`. `settings` (points at the test DB). `ensure_tickers(["AAPL", ...])` - FKs from `watchlist_items/orders/fills/positions/briefs/alerts/proposals` to `tickers` need the symbol to exist.
- `tests/db/rls_specs.py`: `create_prereq(admin)` builds a user + portfolio/proposal/order/brief/thread/trace; `TABLE_SPECS` has a row factory per user table.

**Conventions every module must follow**
1. `app/modules/<name>/router.py` exposes `router: APIRouter` (auto-mounted under `/v1`; module-level `PREFIX = ""` to change; optional `lifespan(app)` async context manager for schedulers/quote stores). Never edit `app/main.py`.
2. Request handlers: `async def h(user: CurrentUser, conn: UserConn, clock: ClockDep, settings: SettingsDep)` from `app.core.deps`. `UserConn` = connection in a transaction with `app.user_id` set (RLS enforced) that **commits before the response is sent** (`scope="function"`). Outside a request: `async with user_tx(uid) as conn` (`app.core.db`). `system_tx()` (owner, RLS bypassed) is for jobs/migrations/seed only - never to serve a user.
3. Errors: `raise AppError(ErrorCode.X, "msg", details={...})` or helpers (`not_found()`, `conflict()`, `validation_error()`, ...). All codes in CONTRACT are in `ErrorCode`. Never return ad-hoc error JSON. Validation errors never echo input.
4. Money/prices: `Decimal` internally; response models use `Money`/`Price`/`Pct` from `app.core.schemas` (serialise as JSON numbers). `ApiModel` (extra=forbid) for request bodies.
5. Every write endpoint appends audit: `await write_audit(conn, user_id=..., action="order.create", entity="order", entity_id=..., details={...})` (no tokens / free text).
6. asyncpg gotchas: jsonb/json params and results are Python dict/list (orjson codec) - never pass a JSON string; vectors are passed as text with `$1::vector` (`vec_literal(list_of_floats)`), not a codec; always parameterised SQL.
7. Time: take `Clock` (`app.state.clock`), never `datetime.now()` in domain code. `PriceSource` Protocol + `StaticPriceSource` are in `app/core/pricesource.py`; stream C extends in `app/modules/marketdata`.
8. Logging: `get_logger(name)`; JSON lines carry `trace_id` (+ `user_id` after auth). Use `extra={...}` keys that do not collide with LogRecord attributes (`module`, `name`, ... are reserved). Never log tokens, prompts with user text, or the query string.
9. Rate limits: per IP in middleware, per user inside `get_current_user` (after the JWT is verified). SSE: mint with `POST /v1/stream/token`, validate with `await consume_stream_token(token, clock.now())` from `app.core.auth` (single use, returns user id or None; runs via a SECURITY DEFINER function so no admin credentials are needed at runtime). Do not read `?token=` into logs.

**Migrations / schema rules**
- Forward-only `migrations/NNNN_name.sql` in your block (A 0100-0199, B 0200-0299, C 0300-0399, release 0900+). Never edit an applied file (checksum check fails - add a new one). No `BEGIN/COMMIT` in files (the runner wraps each in a transaction).
- New **user table** => same migration calls `select sp_apply_user_rls('public.<table>');` (ENABLE+FORCE RLS + policy `tenant_isolation` on `user_id = nullif(current_setting('app.user_id', true),'')::uuid`), has `user_id uuid not null references users(id) on delete cascade`, and parents referenced via **composite FK `(parent_id, user_id)`** like `orders` does. Then add one entry to `TABLE_SPECS` in `tests/db/rls_specs.py`; until you do, `tests/db/test_rls.py` fails (it asserts the list equals every table with a `user_id` column and that every public table is classified). New **reference table** => `select sp_apply_reference_grants('public.<table>')` and add it to `REFERENCE_TABLES` in `rls_specs.py`.
- Default privileges grant the app role SELECT/INSERT/UPDATE/DELETE on every new table; reference/append-only tables explicitly revoke. SECURITY DEFINER functions need an owner that bypasses RLS (migrations refuse to run otherwise) and `set search_path = public, pg_temp`.
- Deviations from SPEC §8 (all intentional): `events` PK is `(user_id, event_id)`; `orders.idempotency_key` unique per `(user_id, key)`; `risk_rules.value`/amounts use `numeric`; `citations`, `fills`, `positions`, `cash_ledger`, `equity_snapshots` carry a denormalised `user_id`; `alerts` has `critical`, `snoozed_until`; `briefs` has `style_hash` (cache key `(user_id, symbol, as_of, style_hash)`), `status`, `stale`; `doc_chunks` carries `form/accession_no/filed_at/quarantined`; `user_interest_vector.baseline`; `proposals.expires_at`; `traces.id` is a uuid PK with the request id in `traces.trace_id`; `llm_calls.user_id` is nullable (system jobs) and **cascades** with the user (spend totals for deleted accounts are lost - revisit if budgets need them).
- `audit_log` is append-only (trigger; the app role has only SELECT/INSERT). It has **no FK to users** on purpose; `anonymize_audit_log(uid)` (SECURITY DEFINER) nulls `user_id` and scrubs `details` once the user row is deleted and only when `app.user_id = uid`. `delete_account()` does delete -> anonymise in one transaction.
- Role `stockpilot_app` is created by migration 0001 with the DEV password `stockpilot_dev_pw`. **Deployments must set `APP_DB_PASSWORD`**; `python -m app.core.migrate` re-applies it with `ALTER ROLE` after migrating, and `DATABASE_URL` must use the same password.

**Auth facts**: `AUTH_MODE=dev` -> `POST /v1/auth/dev-login {persona: priya|alex|sam|new, email?}` (404 otherwise; `APP_ENV=prod` refuses dev mode at boot). Persona ids: `app/core/personas.py` (uuid5-stable; priya `21b7c80a-82d6-5e4d-956e-4a4aed1afc04`). `new` creates a fresh un-onboarded user each call (or find-or-create when `email` is given). `supabase` mode: HS256 (`SUPABASE_JWT_SECRET`) and/or RS256/ES256 (`SUPABASE_JWKS_URL`, cached 10 min, refetch rate-limited), `aud=authenticated`, optional `JWT_ISSUER`; the `users` row is upserted on first authenticated request (`app.core.users.KnownUsers`, 5-min TTL cache).

**Unverified / needs owner**: real Supabase project (JWKS/HS256 paths are tested against locally-minted tokens and a respx-mocked JWKS only); `docker build` and the GitHub Actions workflow (no Docker/Actions here); live Postgres 16 (CI image) - local DB is 15.19, SQL uses PG15 features (`NULLS NOT DISTINCT`, `ON DELETE SET NULL (col)`).
**Next**: BE-04 seed (needs `app/core/personas.py`), then the three Wave-2 streams. Coverage gap worth closing later: `app/core/middleware.py` error-path branches, `ratelimit._evict`.

## 2026-10-31 — orchestrator
Initial scaffolding: agents, workflow, task board, contract. Branch `develop`; owner raises PR to `main`.
Next: Wave 1 (BE-01..03 ‖ FE-01..03), then BE-04 seed.
