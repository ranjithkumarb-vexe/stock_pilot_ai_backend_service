# StockPilot AI runbook

Audience: the owner deploying a beta. Companion docs: `docs/RELEASE.md` (what is and is not ready), `.env.example`, the web repo's
README "Deploy (Vercel)". Everything marked **EXECUTED** was run on 2026-11-02 on a fresh scratch Postgres 15.19 cluster
(own `initdb`, port 55432, pgvector 0.8.0) with the production dependency set (`uv sync --frozen --no-dev`); the results quoted
are the real output. Everything marked **UNVERIFIED** needs an account or tool that was not available (Supabase, Render, Vercel,
Docker, GitHub Actions, the Meta API, Alpaca/Finnhub, a real SEC contact).

Architecture in one line: Vercel (static SPA) -> Render (one FastAPI container, in-process scheduler/SSE/quote store) -> Supabase
(Postgres + pgvector + Auth). One API instance only (ADR-004): never scale `numInstances` above 1 and never add uvicorn `--workers`.

---------------------------------------------------------------------------------------------------------------------------

## 1. Prerequisites

| Need | Why | Notes |
|---|---|---|
| Supabase project (Postgres 15+, pgvector, Auth with Google + TOTP) | database and sign-in | free tier is enough for a beta; note the region |
| Render account (Web Service, paid `starter` plan) | runs the API container | free plans sleep and stop the alert monitor, SSE and order sweeper |
| Vercel account | serves the web app | |
| Google Cloud OAuth client | Google sign-in through Supabase | |
| Meta Model API key | live LLM phrasing | optional: without it the app runs on the labelled mock/degraded path |
| Alpaca (market data) and/or Finnhub key, FRED key | live quotes | optional: without them quotes are `simulated` and labelled so |
| A real contact email for the SEC `User-Agent` | EDGAR ingest + 8-K alert polling | required by the SEC fair-access policy; the client refuses the placeholder |
| Local tools | run migrate/seed/ingest from your machine | Python 3.12, `uv` 0.13+, `psql`/`pg_dump` (client 15+), Node 20+ for the web build |
| GitHub repos on `develop`, PRs to `main` | Render builds `main`, Vercel production branch `main` | PRs are raised by the owner |

## 2. Environment variables and secrets

Source of truth: `.env.example` (every variable the code reads is there; `render.yaml` lists the production set). Never commit values.
Web build variables (`VITE_*`) are public by design; the table below is backend only unless noted.

| Variable | Production value | Where to get it | Secret? |
|---|---|---|---|
| `APP_ENV` | `prod` | fixed. Hides `/docs`, turns MFA enforcement on by default and enables the **production start-up guards** (below): the process exits at boot, listing every problem, when a value is unsafe | no |
| `AUTH_MODE` | `supabase` | fixed. Has no default outside prod (an unset value is a start-up error; prod resolves it to `supabase`); `dev` is refused when `APP_ENV=prod` (SEC-03) | no |
| `REQUIRE_MFA` | `true` (render.yaml sets it; also the prod default) | the API rejects any token whose `aal` claim is not `aal2` (Supabase TOTP verified) with 401 "Multi-factor authentication required". Requires TOTP enabled in Supabase (3.2) and every user to enrol (SEC-04). `false` only as a deliberate choice | no |
| `DATABASE_URL` | `postgresql://stockpilot_app:<APP_DB_PASSWORD>@<host>:5432/postgres` | Supabase -> Project Settings -> Database -> connection string; replace user/password with the `stockpilot_app` role (see 3.2 for pooler user naming) | **yes** |
| `DATABASE_ADMIN_URL` | `postgresql://postgres:<db password>@<host>:5432/postgres` | Supabase -> Database -> connection string (role `postgres`, which must be superuser or BYPASSRLS: migration 0001 refuses otherwise) | **yes, most sensitive**: bypasses RLS. Needed at API runtime too: the paper-order sweeper fills queued orders with it |
| `APP_DB_PASSWORD` | 24+ random chars | generate (`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`). The migrate CLI applies it to role `stockpilot_app`; must equal the password inside `DATABASE_URL`. The dev default `stockpilot_dev_pw` MUST NOT reach production (the app does not refuse it) | **yes** |
| `SUPABASE_URL` | `https://<ref>.supabase.co` | Supabase -> Settings -> API | no |
| `SUPABASE_JWKS_URL` or `SUPABASE_JWT_SECRET` | `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json` (asymmetric keys) or the JWT secret (legacy HS256) | Supabase -> Settings -> API / JWT keys. One of the two is required or boot fails | secret only for HS256 |
| `JWT_ISSUER` | `https://<ref>.supabase.co/auth/v1` | derived | no. **Required in prod** (boot fails without it, SEC-18) |
| `JWT_AUDIENCE` | `authenticated` | Supabase default | no |
| `SUPABASE_SERVICE_ROLE_KEY` | service-role key | Supabase -> Settings -> API. Used only by `DELETE /v1/account` to remove the auth user. Never in the web app | **yes** |
| `CORS_ORIGINS` | `https://<your-app>.vercel.app[,https://custom.domain]` | the Vercel production URL(s); no `*`, no trailing slash | no |
| `TRUST_PROXY` | `true` | behind Render so per-IP rate limits see the client IP | no |
| `TRUSTED_PROXY_HOPS` | `1` | number of trusted reverse proxies in front of the app (Render alone = 1; add 1 for a CDN/WAF in front of Render). The client IP is that-many-th `X-Forwarded-For` entry counted from the **right**; entries to its left are client-controlled and ignored (SEC-08) | no |
| `PORT` | Render sets it (default 10000) | the image binds `$PORT` | no |
| `LLM_PROVIDER` | `mock` until 4.4 passes, then `meta` | | no |
| `META_MODEL_API_KEY` | key | Meta Model API console | **yes** |
| `META_API_BASE_URL`, `MODEL_STANDARD`, `MODEL_CONTRIBUTOR`, `MODEL_DEEP` | defaults in `.env.example` | **UNVERIFIED** against the live API (`docs/VERIFIED.md`) | no |
| `LLM_BUDGET_ALERT_USD` / `LLM_BUDGET_HARD_STOP_USD` / `LLM_USER_DAILY_BUDGET_USD` | `100` / `140` / `1.00` | your budget; see section 10 | no |
| `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK` | real price list | **placeholders** (1.00/4.00) until you copy the Meta price list; budgets are only as accurate as these | no |
| `MARKET_DATA_SOURCE` | `simulated` (default) / `alpaca` / `finnhub` | switch only after 4.5 | no |
| `ALPACA_KEY_ID`, `ALPACA_SECRET`, `FINNHUB_KEY`, `FRED_API_KEY` | keys | Alpaca / Finnhub / FRED dashboards (market-data scopes only) | **yes** |
| `SEC_USER_AGENT` | `"StockPilot you@yourdomain"` (quoted) | your real contact. **Required in prod** (the placeholder is refused at boot) | no |
| `ALERTS_EDGAR_LIVE` | `1` once `SEC_USER_AGENT` is real, else `0` | | no |
| `RATE_LIMIT_*`, `DB_POOL_*`, `MAX_BODY_BYTES`, `REQUEST_TIMEOUT_S`, `DB_STATEMENT_TIMEOUT_MS`, `STREAM_TOKEN_TTL_S` | defaults | `.env.example` | no |
| `LANGFUSE_*`, `REDIS_URL` | empty | not wired to a backend; ignore | no |
| `DEV_JWT_SECRET` | leave unset in prod | only read when `AUTH_MODE=dev`; in prod the public default is replaced by a random per-process value | - |

**Production start-up guards (SEC-02, SEC-03, SEC-18, CR-REL-2).** With `APP_ENV=prod` the app refuses to boot (one message, every problem) when:
`AUTH_MODE=dev`; `CORS_ORIGINS` contains `*`; no Supabase JWT secret / JWKS URL; `APP_DB_PASSWORD` is empty or the dev default `stockpilot_dev_pw`;
the `DATABASE_URL` password is missing or the dev default; `DATABASE_ADMIN_URL` still has the well-known `postgres:postgres` style password;
`JWT_ISSUER` is empty; `SEC_USER_AGENT` is the placeholder / has no e-mail; `LLM_PROVIDER=meta` without `META_MODEL_API_KEY`;
`MARKET_DATA_SOURCE=alpaca` without both Alpaca keys or `finnhub` without `FINNHUB_KEY`. `make prod-check` proves each refusal in a fresh process.
Note for Supabase: the admin role is the `postgres` role with your own database password, so the check passes; it only rejects the literal defaults.

Shell note: values with spaces must be quoted (`SEC_USER_AGENT="StockPilot you@x.com"`) when a file is sourced by a shell
(found while rehearsing: an unquoted value broke `source prod.env`). Render/Vercel dashboards take the raw value.

## 3. Database

### 3.1 Local rehearsal (EXECUTED)
```bash
# fresh scratch cluster + DB, production dependency set, production-like env (fake secrets), network blocked for SEC
uv sync --frozen --no-dev                       # what the image installs
createdb stockpilot_rb                           # or the managed DB already exists
set -a; . ./prod.env; set +a                     # APP_ENV=prod AUTH_MODE=supabase DATABASE_ADMIN_URL=... APP_DB_PASSWORD=<random>
uv run python -m app.core.migrate
```
`prod.env` must be a COMPLETE production configuration (section 2): the migrate CLI loads the same settings, so the start-up guards also stop a half-filled file here.
Result: `migrations applied: [1, 2, 3, 4, 5, 6, 7, 8, 100, 101, 200, 201, 300, 301, 302, 400, 500, 900]` (900 = account tombstones, SEC-13; re-run 2026-11-02 on a fresh DB) in 0.3 s; a second run prints
`migrations applied: none (up to date)`. Post-migrate checks (run them after every deploy that migrates):
```sql
-- as the admin role
select extname, extversion, nspname from pg_extension e join pg_namespace n on n.oid=e.extnamespace
 where extname in ('vector','pgcrypto','pg_trgm','uuid-ossp');            -- 4 rows, schema public
select rolname, rolsuper, rolbypassrls from pg_roles where rolname='stockpilot_app';   -- f | f
select count(*), count(*) filter (where relrowsecurity), count(*) filter (where relforcerowsecurity)
  from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind='r';  -- 38 | 29 | 29
select max(version) from schema_migrations;                                  -- 500
```
Observed: 4 extensions in `public`, `stockpilot_app` not super/BYPASSRLS, 38 tables of which 29 (every table with `user_id`) have
RLS enabled and forced, `schema_migrations` 17 rows / max 500. As the app role: `'[1,2,3]'::vector <-> '[1,2,4]'::vector` works,
`insert into tickers ...` is `permission denied` (reference tables are read-only to the app), the dev password `stockpilot_dev_pw`
is rejected (`password authentication failed`), and `select count(*) from users` without `app.user_id` returns 0 (RLS).

### 3.2 Supabase specifics (UNVERIFIED: no project available)
1. Create the project; set a strong database password. Pick the region next to the Render region.
2. Before the first migrate, pre-create the extensions in `public` so the app role resolves the `vector` type without a custom
   search_path: `create extension if not exists vector with schema public;` (and `pgcrypto`, `pg_trgm`, `"uuid-ossp"`). After
   migrating, connect as `stockpilot_app` and run `select '[1,2]'::vector;` - if it fails with "type vector does not exist",
   the extension is in `extensions`; fix with `alter role stockpilot_app set search_path = public, extensions;`.
3. Confirm `postgres` can run migration 0001: `select rolsuper, rolbypassrls from pg_roles where rolname = current_user;` must show
   one `t`. If not, the migration aborts with "migrations must run as a superuser or BYPASSRLS role" - nothing is applied.
4. Connection strings: use the Supavisor **session** pooler (port 5432) or the direct host. Do not use the transaction pooler
   (port 6543): asyncpg prepared statements and `set_config('app.user_id', ..., true)` assume session semantics. With the pooler,
   user names are `<role>.<project-ref>` (so `stockpilot_app.<ref>`); the direct host `db.<ref>.supabase.co` is IPv6-only on some plans
   - if Render cannot reach it use the pooler. Add `?sslmode=require` if the driver demands TLS (asyncpg: `ssl=require`).
5. Auth -> Providers -> Google (client id/secret from Google Cloud; authorised redirect URI = the Supabase callback URL shown there).
   Auth -> URL Configuration: Site URL = Vercel production URL; Redirect URLs include `https://<vercel>/signin`.
   Auth -> MFA: enable TOTP.

### 3.3 Migrations: policy, rollback, roll forward
Migrations are **forward-only** SQL (`migrations/NNNN_name.sql`), each in its own transaction with its `schema_migrations` row; an
applied file whose checksum changed is refused; a failing file rolls back completely and is not recorded (tests in
`tests/db/test_migrations.py`). There is no `down`. Procedure for every release that contains a new migration:

1. **Back up first** (3.4). Note the current `max(version)`.
2. Run the migration **before** deploying the new API (`uv run python -m app.core.migrate` from your machine, or Render
   `preDeployCommand: python -m app.core.migrate` on paid plans). New migrations must be backward compatible with the running API
   (additive: new tables/columns/indexes); destructive changes ship in two releases (expand, then contract).
3. Deploy the API. Smoke (section 11).
4. If the release is bad: **roll the application back** (Render -> Deploys -> redeploy the previous image). Because migrations are additive
   the old code keeps working on the new schema. Do **not** try to un-apply a migration.
5. If the schema itself is wrong: **roll forward** with a new migration (next free number, `0900+` block is reserved for release
   fixes) that corrects it. If data is damaged, restore from backup (3.4) into a new database and repoint `DATABASE_URL`.

### 3.4 Backup and restore (EXECUTED locally)
```bash
pg_dump -Fc -d "$DATABASE_ADMIN_URL" -f stockpilot_$(date +%F).dump          # logical backup (owner role)
createdb stockpilot_restore && pg_restore -d <restore-dsn> --no-owner stockpilot_2026-11-02.dump
DATABASE_ADMIN_URL=<restore-dsn> uv run python -m app.core.migrate           # no-op, re-applies the app-role password
```
Rehearsed: 208 KB dump, `pg_restore` exit 0, counts identical (1 user, 1 order, 18 tickers, migrations 500), migrate on the restored
DB printed `none (up to date)`, and the app role could connect and still saw 0 rows without `app.user_id`. The role
`stockpilot_app` is cluster-level and is not in a database dump: on a new server run migrate (it creates the role and sets the
password) *before* or right after `pg_restore`. Supabase also takes daily backups on paid plans and offers point-in-time recovery
as an add-on (**UNVERIFIED**, check your plan); the owner should still take a `pg_dump` before every migration.

## 4. First deploy (order matters)

### 4.1 Supabase
Section 3.2 steps 1-5. Record: project ref, region, DB password, JWKS URL, anon key (public), service-role key (secret).

### 4.2 Database schema and reference data (from your machine, once)
```bash
cp .env.example .env     # NOT committed; fill DATABASE_ADMIN_URL, DATABASE_URL, APP_DB_PASSWORD, SEC_USER_AGENT only
uv sync --frozen --no-dev
uv run python -m app.core.migrate                     # section 3.1
SEC_USER_AGENT="StockPilot you@yourdomain" uv run python -m app.modules.edgar.cli ingest-xbrl            # live SEC, ~16 tickers
SEC_USER_AGENT="StockPilot you@yourdomain" uv run python -m app.modules.research.cli ingest-filings      # 10-K + 8-K -> chunks
```
(`make migrate`, `make ingest-xbrl`, `make ingest-filings` are the same commands.) Ingest needs the universe in `tickers` first,
which only `scripts.seed` writes - see section 6. Ingest results were last observed on the dev DB (BE-11: 16/16 tickers,
67k facts; BE-12: 79 filings, 1,748 chunks, 519 tables, live SEC, <= 8 req/s). **On the scratch DB the two ingest commands were
executed with the network blocked and failed cleanly**: per-ticker JSON `error: EdgarUnavailableError: giving up on
https://data.sec.gov/... after 6 attempts (ConnectError)`, nothing written. They were deliberately not run live because no real
SEC contact was available (the SEC policy requires a real one); treat the live ingest on a fresh production DB as **UNVERIFIED**.
Without ingest the app still works: briefs say "No annual report (10-K) has been indexed for this company yet" and are marked stale
(EXECUTED in 7).

### 4.3 Render API
1. Dashboard -> New -> Blueprint -> pick the backend repo (`render.yaml`), branch `main`. Fill every `sync: false` variable (section 2).
   Leave `LLM_PROVIDER=mock`, `MARKET_DATA_SOURCE=simulated`.
2. Deploy. Health check is `/readyz`. Note the real URL (`https://<name>.onrender.com`).
3. If that URL differs from `https://stockpilot-api.onrender.com`, edit the web repo's `vercel.json` `connect-src` and set
   `VITE_API_BASE_URL` accordingly (the web build fails in `check:deploy` otherwise).
4. Verify (from anywhere): `curl -s https://<api>/readyz` -> `{"status":"ok"}`; `curl -s -o /dev/null -w "%{http_code}" -X POST https://<api>/v1/auth/dev-login` -> `404`;
   `https://<api>/docs` -> `404`.

### 4.4 Meta LLM (optional, after 4.3)
`LLM_PROVIDER=meta META_MODEL_API_KEY=... uv run python -m app.modules.llm.smoke` (exit 2 = blocked: no key). Only if it prints
a successful call, set `LLM_PROVIDER=meta` + the key on Render and redeploy. Then generate one brief and read it: the prompt/response
shapes were never exercised live (`docs/VERIFIED.md`). Fill in the real `LLM_PRICE_*` first.

### 4.5 Live market data (optional)
Set `ALPACA_KEY_ID/ALPACA_SECRET` (and/or `FINNHUB_KEY`), `MARKET_DATA_SOURCE=alpaca`, redeploy, watch `GET /v1/market/status`
(`feed_state`, `source`). Caveat: daily price history (`prices_daily`) is the seeded simulated series; no job refreshes it from Alpaca,
so risk/performance math keeps using simulated history even when quotes are live (`docs/RELEASE.md` defects).

### 4.6 Vercel web app
Follow the web README "Deploy (Vercel)": env vars from `.env.production.example`, API origin in `vercel.json`, then add the Vercel URL
to the backend `CORS_ORIGINS` (redeploy API) and to Supabase redirect URLs.

### 4.7 DNS (only with custom domains)
`app.<domain>` CNAME -> Vercel, `api.<domain>` CNAME -> Render. Then update, in this order: Render `CORS_ORIGINS`, web
`vercel.json` `connect-src` + `VITE_API_BASE_URL`, Supabase Site/Redirect URLs, Google OAuth authorised origins. Redeploy both.

### 4.8 First user
Open the Vercel URL -> "Continue with Google" -> (TOTP enrolment/verify) -> onboarding. In the Supabase dashboard confirm the user,
then `select count(*) from users;` as admin shows 1 (auto-provisioned on first authenticated request, EXECUTED in 7).

## 5. Render configuration notes
`render.yaml`: Docker runtime, `starter` plan, `numInstances: 1`, `autoDeploy: false`, health check `/readyz`, `TRUST_PROXY=true`.
Logs: Render dashboard -> Logs (JSON lines). UNVERIFIED: the Blueprint file was not validated by Render.

## 6. Seeding: demo personas must NOT go to production
`make seed` (`python -m scripts.seed`) writes (a) the supported universe (`tickers`, 18 rows incl. SPY) and 120 days of **simulated**
`prices_daily` (source `simulated`, "through" the seed date), and (b) the three demo personas priya/alex/sam (users with
`@stockpilot.dev` emails, portfolios, events, proposals, alerts). **Production: use `--universe-only`** (alias `--personas none`): it writes
only (a), no users, proposals or alerts (CR-REL-1, EXECUTED on a fresh scratch DB: `users 0, tickers 18, prices_daily 2160`, idempotent).

```bash
uv run python -m scripts.seed --universe-only
# -> seed ok (prices through 2026-10-30, source=simulated): 18 tickers, 2160 price rows; personas: none (universe only)
```
If demo personas were seeded by mistake (or an environment started as a demo), remove them with SQL (EXECUTED on the scratch DB earlier):
```bash
uv run python -m scripts.seed          # (only to reproduce the old situation) personas: priya=seeded, alex=seeded, sam=seeded
psql "$DATABASE_ADMIN_URL" <<'SQL'
begin;
delete from users where email in ('priya@stockpilot.dev','alex@stockpilot.dev','sam@stockpilot.dev');   -- cascades to all their rows
commit;
SQL
```
Verified afterwards: `users = 0`, `tickers = 18`, `prices_daily = 2160`, and the only table still holding `user_id` rows is `audit_log`
(13 rows): it is append-only by design and has no FK to users. Those rows reference persona ids only. Skip the persona cleanup only for
a demo/staging environment. Persona accounts can never sign in on a production deployment anyway (`AUTH_MODE=supabase`;
`POST /v1/auth/dev-login` is 404), but they would pollute data.

Disabling dev auth = `APP_ENV=prod` + `AUTH_MODE=supabase` (and `AUTH_MODE` is never defaulted to `dev`: leave it unset and local runs fail with "AUTH_MODE is not set"; the app refuses to boot with `APP_ENV=prod AUTH_MODE=dev`, EXECUTED:
`Value error, AUTH_MODE=dev is not allowed when APP_ENV=prod`; same for `CORS_ORIGINS=*`, and supabase mode without a JWT
secret/JWKS URL). The dev market controls (`/v1/dev/market`, `/v1/dev/feed`, `/v1/dev/recompute-features`) check `AUTH_MODE=dev` too
(answer 404 for authenticated callers; 401 without a token).

## 7. Local production-mode start and demo script (EXECUTED)
`make prod-check` (`scripts/prod_start_check.py`) automates the following against a scratch database and tears it down:
```
ok   GET /healthz -> 200            ok   GET /readyz -> 200
ok   POST /v1/auth/dev-login -> 404 ok   GET /docs -> 404          ok   GET /openapi.json -> 404
ok   GET /v1/me without token -> 401  ok   wrong-secret token -> 401   ok   valid Supabase-style HS256 token -> 200
ok   CORS preflight from the configured origin allowed / foreign origin refused    ok   security headers
prod-check: OK, 11 probes
```
Manual run (uvicorn exactly like the container, `uv run --frozen --no-dev uvicorn app.main:create_app --factory --port 8020`):
boot to ready in 4 s, response headers `x-content-type-options: nosniff`, `x-frame-options: DENY`, HSTS `max-age=63072000`, CSP
`default-src 'none'; frame-ancestors 'none'`, `cache-control: no-store`.

### Beta-user test script (works against any deployment; replace `$API` and `$TOK` = the user's Supabase access token)
Executed against the local prod-mode server with a token minted from the fake JWT secret:
1. `GET /v1/me` -> 200, `onboarded:false` (user auto-created).
2. `GET /v1/onboarding/questions` -> 6 questions; answers must be **option scores** (Q1 [1,2,4,5], Q2 [1,2,3,5], Q3 [1,3,4], Q4 [1,2,3,5], Q5 [1,3,5], Q6 [2,3,5]).
3. `POST /v1/onboarding` `{"answers":[4,3,4,3,3,3],"horizon":"3-10 years","goals":["Growth"],"experience":"Intermediate","disclaimer_ack":true,"watchlist":["NVDA","JNJ","KO"],"likes":["Technology"],"excludes":[]}` -> 200 tier 3 "Balanced" (a wrong score gives 422 `validation_error` naming the question).
4. `GET /v1/portfolio` -> cash 100000, equity 100000; `GET /v1/portfolio/risk` -> rules pos 15, sec 35, dd 15, trades 5, cash 5.
5. `GET /v1/watchlist` -> NVDA/JNJ/KO quotes `closed`, source `simulated`; `GET /v1/market/status` -> `simulated:true`.
6. `POST /v1/orders/preview` JNJ buy 10 market -> `will:"queue"` (market closed), checks array. `POST /v1/orders` needs `Idempotency-Key: <uuid>` (a non-UUID key is 422) -> 201 `status:"queued"`.
7. `GET /v1/briefs/NVDA` -> 200, `stale:true`, "No annual report (10-K) has been indexed" until ingest has run. `POST /v1/chat` `{"message":"Should I buy NVDA?"}` -> refusal + decision framework (never advice). `GET /v1/feed` (3 items), `/proposals`, `/alerts`, `/audit/traces` (2 after the above), `/evals/latest`, `/profile/export` -> 200.
Human beta script (with the web app): sign in with Google (+TOTP), finish onboarding < 3 min, open the watchlist and a brief (check the
"simulated/delayed/stale" badges and the disclaimer), preview and place a paper order, approve/reject a rule-breach proposal, open "Why am
I seeing this?", ask the chat "should I buy X" (must refuse), open Profile -> export and forget a memory, Audit -> Evals.

## 8. Health checks and monitoring
| Check | Endpoint | Healthy | Meaning of failure |
|---|---|---|---|
| Liveness | `GET /healthz` | 200 `{"status":"ok"}` | process down; Render restarts it |
| Readiness | `GET /readyz` | 200 | 503 `upstream_unavailable`: database unreachable (check Supabase status, `DATABASE_URL`, pool) |
| Market feed | `GET /v1/market/status` (auth) | `feed_state: live`, `source` as configured | `stale`/`delayed`: vendor stream down; the API falls back WS -> REST -> cache and blocks market fills when stale |
| Docker | image `HEALTHCHECK` (python urllib on `/healthz`) | healthy | UNVERIFIED |
Add an external uptime monitor (UptimeRobot, Better Stack, ...) on `/readyz`, 1-5 min.

Logs are JSON lines on stdout. Fields: `ts`, `level`, `logger`, `msg`, `trace_id`, and for requests (`logger=app.access`, `msg=request`)
`user_id`, `method`, `path`, `status`, `duration_ms`. Startup lines: `module mounted`, `startup complete` (`db_degraded`, `env`). Tokens, prompts
with user text and query strings are never logged. Every API response carries `X-Trace-Id`; ask a tester for it, then
`grep <trace_id>` the logs, or open the trace in the app (Audit -> Traces). What to alert on: `level=ERROR`, `status>=500`, `status=429` bursts,
`duration_ms` p95 > 1500 on `/v1/portfolio` and `/v1/feed` (NFR-01), `/readyz` failing. Langfuse/OpenTelemetry are not wired (variables exist only).

## 9. Docker (UNVERIFIED; no Docker on the authoring machine)
Static review of `Dockerfile`: multi-stage; uv pinned (0.13.6) with `--frozen --no-dev`; final image `python:3.12-slim`, user `app` uid 10001
(`USER app`), no secrets copied (`.dockerignore` excludes `.env*`, tests, docs, `.git`), healthcheck via python (no curl needed), one
uvicorn process, `$PORT` honoured. A defect found and fixed in review: `evals/results` was not copied, so `GET /v1/evals/latest`
(Audit -> Evals) would have been empty in the container.
Substitute check (EXECUTED): the exact COPY set (`pyproject.toml uv.lock app migrations scripts evals/results`) was reproduced in a clean
directory, `uv sync --frozen --no-dev --no-install-project` run there, migrations applied, uvicorn started with `--port ${PORT:-8000}` ->
`/readyz` ok, `/v1/evals/latest` served from the copied results, the healthcheck command exited 0. First CI run of the `docker` job in
`.github/workflows/ci.yml` (build, non-root, migrate, prod boot, dev-login 404) is the real test. Compose: `infra/docker-compose.yml`
(`docker compose -f infra/docker-compose.yml up --build`; profiles `seed`, `web`) - YAML parses, never started.

## 10. Cost controls (LLM)
Layers, all enforced in code from `llm_calls`: per-user daily budget `LLM_USER_DAILY_BUDGET_USD` (default 1.00; beyond it that user gets
template/degraded answers), global alert `LLM_BUDGET_ALERT_USD` (100) and hard stop `LLM_BUDGET_HARD_STOP_USD` (140), default
`LLM_REASONING_EFFORT=low`, brief cache by (user, symbol, day, style), Contributor-mode model only for public-data jobs. The
spend numbers are only as good as `LLM_PRICE_IN/OUT_PER_MTOK` (placeholders). Check spend:
`select date_trunc('day', created_at) d, sum(cost_usd), count(*) from llm_calls group by 1 order by 1 desc;`
Emergency stop: set `LLM_PROVIDER=mock` and redeploy (the app keeps working in degraded mode). Also set a spend limit in the Meta console.

## 11. Smoke test after every deploy
`/readyz` 200, dev-login 404, sign in with Google, `GET /v1/me` 200, dashboard loads with data, `GET /v1/market/status` shows the expected `source`,
one brief opens, one paper order previews. Web: `npm run e2e:deploy` passed before pushing.

## 12. Rotating secrets
| Secret | Procedure |
|---|---|
| `APP_DB_PASSWORD` | set the new value in Render (`APP_DB_PASSWORD` and the password inside `DATABASE_URL`) and run `uv run python -m app.core.migrate` with the new value (it executes `ALTER ROLE stockpilot_app PASSWORD`); redeploy. Brief downtime between the two steps: do them together |
| Supabase DB password (`postgres`) | rotate in Supabase, update `DATABASE_ADMIN_URL` in Render, redeploy |
| Supabase JWT secret / signing keys | rotate in Supabase; for JWKS projects nothing to change (cached 10 min, `JWKS_CACHE_TTL_S`); for HS256 update `SUPABASE_JWT_SECRET`, redeploy; all sessions are invalidated |
| `SUPABASE_SERVICE_ROLE_KEY`, `META_MODEL_API_KEY`, `ALPACA_*`, `FINNHUB_KEY`, `FRED_API_KEY` | create a new key at the vendor, update Render, redeploy, revoke the old key |
| Web `VITE_SUPABASE_ANON_KEY` | public value; after a Supabase key change update Vercel and redeploy |
| A leaked secret | revoke at the vendor first, rotate, then `gitleaks git .` and rewrite history only with the owner's explicit decision |

## 13. Incident basics
1. Is it up? `/healthz`, `/readyz`, Render status, Supabase status, Vercel status.
2. 5xx: take the `trace_id` from the user's screen ("trace reference"), grep Render logs, look for `ERROR`.
3. DB down: `/readyz` 503; the API stays up and recovers when the DB returns. Do not restart repeatedly.
4. Bad release: Render -> redeploy previous deploy (schema is additive, section 3.3). Web: Vercel -> promote previous deployment.
5. LLM misbehaving or overspending: `LLM_PROVIDER=mock`, redeploy.
6. Market data wrong/stale: `MARKET_DATA_SOURCE=simulated` (clearly labelled), redeploy.
7. Suspected data exposure: rotate secrets (section 12), check `audit_log` (`select action, count(*) ... group by 1`), export affected user data (`/v1/profile/export`) only on request; remember RLS is enforced for the app role only.
8. Record what happened in `docs/workflow/HANDOFF.md`.
