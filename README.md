# StockPilot AI - backend

FastAPI + Postgres (pgvector) service for StockPilot AI: personalized equity research and **paper-trading only**
decision support. Spec: `docs/SPEC.md`. Task board and agent workflow: `docs/workflow/`. API contract:
`docs/workflow/CONTRACT.md` and `docs/contracts/openapi.json`.

## Quick start (macOS/Linux, Python 3.12, `uv`, Postgres 15+ with pgvector)

```bash
cp .env.example .env        # edit DATABASE_URL / DATABASE_ADMIN_URL for your machine
make setup                  # uv sync, create dev + test databases, role stockpilot_app, run migrations
make test                   # pytest + coverage (uses database $SP_TEST_DB, default stockpilot_test)
make lint                   # ruff + mypy --strict
make run                    # http://127.0.0.1:8000  (docs at /docs when APP_ENV != prod)
make openapi                # regenerate docs/contracts/openapi.json
make seed                   # universe + personas + demo data (BE-04)
```

Try it:

```bash
curl localhost:8000/healthz
curl -s -X POST localhost:8000/v1/auth/dev-login -H 'content-type: application/json' -d '{"persona":"priya"}'
curl -s localhost:8000/v1/me -H "Authorization: Bearer <access_token>"
```

## Deploy and release
`docs/RUNBOOK.md` (prerequisites, env/secrets table, first deploy on Supabase + Render + Vercel, migrations/backup/rollback, monitoring,
cost controls, incident basics, beta test script), `docs/RELEASE.md` (gate table, owner checklist, unverified items, known defects),
`Dockerfile`, `render.yaml`, `infra/docker-compose.yml`. `make prod-check` boots the API in production mode (no Docker) and probes it;
`make docker-build` / `make compose-up` need Docker (never run on the authoring machine).

## Layout

```
app/main.py            create_app(): middleware stack, module auto-discovery (app/modules/*/router.py)
app/core/              config, db (pools, user_tx/system_tx), auth, errors, middleware, ratelimit, clock, pricesource, migrate, deps, audit
app/modules/<name>/    feature modules; expose `router` (mounted under /v1 unless PREFIX is set)
migrations/NNNN_*.sql  forward-only SQL, applied by `python -m app.core.migrate`
scripts/               setup_db, dump_openapi, seed
tests/                 conftest fixtures (db_admin, db_as_user, app_client, make_user, auth_headers, fake_clock)
```

## Security model in one paragraph

The API connects as the non-superuser role `stockpilot_app`; every user table has FORCED row-level security keyed on
`app.user_id`, which `user_tx(user_id)` sets per transaction. The owner/admin URL is only for migrations, seeding and
background jobs (`system_tx`). `AUTH_MODE` has no default (unset = start-up error outside prod, `supabase` in prod); `dev` (local only; refused when `APP_ENV=prod`; `.env.example` and `make` set it) issues dev JWTs; `supabase` verifies
Supabase JWTs. The deployment must set `APP_DB_PASSWORD` (the dev default `stockpilot_dev_pw` is for local use only).
