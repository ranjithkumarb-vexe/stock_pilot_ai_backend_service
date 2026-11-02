# StockPilot AI: release readiness (REL-01)

_Written 2026-11-02 by release-manager; updated the same day by the BE-22 hardening agent (this revision). Both repos on `develop`, nothing committed or deployed by these reports. Procedures live in
`docs/RUNBOOK.md`; the web repo has a pointer copy at `docs/RELEASE.md`._

## Release status

**NOT READY by the written gate (WORKFLOW section 6): gates 1 and 8 are not fully met. All engineering blockers are closed.**

Why not "READY FOR BETA (owner config required)" yet:
1. **Gate 1 (every P0 task `done` or explicitly `deferred`)**: QA-01 is now `done` (SPEC 12.4 matrix in `docs/qa/SCENARIO_MATRIX.md`: 39 covered-pass, 5 covered-partial, 1 not-measurable-here, 0 not-covered); BE-09, BE-13, BE-14, BE-16, BE-17 and SEC-01 are `partial` because their live-service parts (Alpaca/Finnhub, Meta model, live EDGAR polling, real Supabase) cannot be verified without owner credentials. Only the owner can mark those `deferred`.
2. **Gate 8 (RUNBOOK verified step by step)**: the local parts were executed (now incl. migrations 0001-0900 on a fresh DB, `--universe-only` seed, 19-probe prod-mode check); the Supabase, Render, Vercel, Docker and CI parts are written but unexecuted.

What changed since the previous revision of this report (BE-22): the backend suite is green (CR-REL-5 fixed: 1571 passed, 0 xfailed, 95% coverage), `evals/results/latest.json` is a full run and `--fast` can no longer overwrite it (CR-REL-3), **all 6 medium security findings are fixed** (SEC-02, 03, 04, 08, 11, 12) together with SEC-06, 07, 10, 13, 17, 18, 20 and CR-REL-1/2; ruff, ruff format and mypy are clean repo-wide; cross-repo `npm run e2e` re-run: 38/38. **New honest findings**: the dashboard load smoke is borderline (page-load p95 1.24-1.52 s at 50 users against the 1.5 s budget; 2.7 s at 100 users, dev laptop, single process); the new MFA enforcement (`aal2`) and the tombstone/`iat` logic were verified with locally minted tokens only.
If the owner accepts the partial tasks as deferred (or verifies them) and works through section 4, the status becomes **READY FOR BETA (owner config required)**; nothing in this report has been deployed.

## 1. Gate table (WORKFLOW.md section 6)

| # | Gate | State | Evidence |
|---|---|---|---|
| 1 | Every P0 task `done` or deferred with reason | **NOT MET** | `docs/workflow/TASKS.md`: BE-22 and QA-01 `done` (QA-01: `docs/qa/SCENARIO_MATRIX.md`, 45 scenarios, 0 not-covered); BE-09/13/14/16/17 and SEC-01 `partial` (live services / owner items). Web: 13/13 done |
| 2 | Backend `make test` green | **MET** | 2026-11-02, `SP_TEST_DB=stockpilot_test_hard uv run pytest --cov=app`: **1583 passed, 0 failed, 1 xfailed (QA-D1)**, 95% coverage; also green with `tests/security` before and after `tests/trading`. `ruff check`, `ruff format --check`, `mypy` clean repo-wide. VERIFICATION rows `BE-22`, `BE-22 / CR-REL-5` |
| 3 | Frontend `npm run test && npm run build` green | **MET** | web VERIFICATION rows `FE-12` and `INT-03` (2026-11-02): 280 vitest passed, lint/typecheck clean, build ok, `check:bundle` and `check:deploy` ok |
| 4 | Cross-repo E2E green | **MET** | web VERIFICATION (2026-11-02, after the BE-22 changes): `npm run e2e` **38 passed** (4.6 min) against the real backend + production build. Three specs encoded old behaviour (deleted-account token still working, a guaranteed failing eval, a hard-coded simulated portfolio value) and were corrected first; see the VERIFICATION row `BE-20 / REL-01 gate 4 (re-run)` |
| 5 | No high/critical open in `docs/security/` | **MET** | `docs/security/FINDINGS.md`: 0 critical, 0 high, **0 medium open**. Open/accepted: SEC-01 (admin pool in the API process), 05, 16, 22; info 09, 14, 19, 21 |
| 6 | Migrations apply on an empty database | **MET** | RUNBOOK 3.1 (EXECUTED): 17 migrations on a fresh PG 15.19 cluster, rerun = none (`BE-21`); re-run 2026-11-02 on a fresh database with the new `0900_account_tombstones`: 18 migrations `[1..8, 100, 101, 200, 201, 300, 301, 302, 400, 500, 900]` (VERIFICATION `BE-22 / SEC-06...`) |
| 7 | `.env.example` complete | **MET** | every `Settings` field and direct `os.environ` read is listed (re-checked 2026-11-02 incl. `TRUSTED_PROXY_HOPS`, `REQUIRE_MFA`, `RATE_LIMIT_EXPENSIVE_*`); `tests/test_config_hardening.py::test_env_example_loads_and_is_explicit_dev` loads the file; `render.yaml` adds `REQUIRE_MFA` and `TRUSTED_PROXY_HOPS` |
| 8 | RUNBOOK verified step by step | **PARTIAL** | local parts executed (migrate, seed incl. `--universe-only`, persona cleanup, prod-mode boot with the new start-up guards, backup/restore, image file layout). Supabase, Render, Vercel, Docker, live ingest, live LLM/market data are written but not executed |
| 9 | RELEASE lists blockers/credentials/unverified items honestly | **MET** | this file |

## 2. Task tallies (from the two TASKS.md files)
- Backend (`docs/workflow/TASKS.md`, 25 tasks): **19 done, 6 partial, 0 blocked, 0 todo**. Partial: BE-09 (live Alpaca/Finnhub unverified), BE-13 (no live Meta call), BE-14 (LLM-phrased path, citation support, streaming), BE-16 (synthetic personas), BE-17 (live EDGAR polling/market hours), SEC-01 (review done and fixes landed; real Supabase / ZAP / Semgrep / live model not verified). BE-22 and QA-01 are done. **QA-01 findings that touch the gates**: (a) gate 2's evidence line is stale: re-run at ed0300e with the new `tests/qa` = 1579 passed, **2 failed**, 3 xfailed; the 2 failures are `tests/security/test_ssrf_static.py::test_no_secrets_in_tracked_files` / `::test_no_secrets_in_git_history` against a fake credential literal in `tests/security/test_input_abuse.py:162` (QA-D3, one-line test fix, not applied by QA); (b) QA-D1 (interest-drift prompt never served) and QA-D2 (an outage replaces the cached good brief; degraded briefs cached for the day) are open product gaps against SPEC T-PERS-03 / T-CHAOS-01, see `docs/qa/SCENARIO_MATRIX.md` section 5.
- Web (`docs/workflow/TASKS.md`, 13 tasks): **13 done**.
- Release tasks done: FE-12, BE-21, REL-01, INT-03, BE-22, QA-01.

## 3. Test and eval scoreboard
| Suite | Result |
|---|---|
| Backend pytest (full, 2026-11-02, after BE-22) | **1583 passed, 0 failed, 1 xfailed (QA-D1)**, coverage 95% of `app/` (CI floor 80%; risk ~99%, paper ~97%, personalization ~96%) |
| Backend security regression suite (`tests/security`) | all green, no xfail left; every fixed finding has a named test (FINDINGS.md) |
| `ruff check` / `ruff format --check` / `mypy --strict` | clean repo-wide (342 files / 306 source files) |
| `make prod-check` | **19/19 probes** (adds: aal1 token 401, X-Forwarded-For rotation still rate-limited, six "refuses to start" probes) |
| Load smoke (`scripts/load_smoke.py`, dev laptop, one process, simulated data) | 1 user: page load 29 ms. **50 users: page-load p95 1244 / 1519 / 1340 ms (budget 1500, borderline)**, 0 errors. 100 users: 2696 ms. Brief generation latency not measured |
| Web vitest | 280 passed (24 files), lint + typecheck clean |
| Web live E2E (`npm run e2e`, real backend) | **38 passed** (4.6 min), 2026-11-02 after the BE-22 changes |
| Web deploy smoke (`npm run e2e:deploy`) | 6 passed, 0 CSP violations (INT-03; not re-run, no web build change) |
| Bundle | initial JS 125.7 KB gzip, total 315.8 KB (budgets 160 / 400) |
| gitleaks / pip-audit / npm audit | clean in both repos / clean (reviewer) / 2 moderate (react-router) - not re-run in BE-22 |

### Eval scoreboard (`evals/results/latest.json`, SPEC 12.3)
Final full run (`make eval`, 2026-11-02T09:06Z, mock LLM, git_sha c0a8cb2 = HEAD with a dirty tree): **38 pass, 0 fail, 4 not measured, `thresholds_met: true`**. Highlights: T-RAG-01 30/30, T-RAG-02 0 unverified, T-RAG-03 100% citation coverage, T-RAG-04 proxy 1.00 / 0.0%, **T-SEC-01 25/25 injections quarantined, 0 false positives on the benign set and 0 of 1,748 real filing chunks flagged**, **T-SEC-02 52/52 (set grew from 30; includes obfuscated and paraphrased asks), 0 guarantee phrases**, T-PERS-02 NDCG 0.89, T-PERS-03 100% (median 13 events), **T-PERS-TTP 27 events (<= 30; the old committed run failed at 52)**, T-PERS-AB lift positive, risk/paper/data/alert/chaos checks pass. Not measured: T-RAG-03b and T-RAG-04b (need a live judge model), T-PERF-01 (load smoke exists, not automated), T-A11Y-01 (axe is covered by the web specs: 0 violations, not wired into this file). `python -m evals.run --fast` now writes `latest.fast.json` and refuses `--out latest.json`. All numbers are from MockProvider/offline paths on synthetic personas.

## 4. Owner action checklist (in order)
1. **Supabase**: create the project (note region); pre-create extensions in `public`; confirm the `postgres` role is superuser/BYPASSRLS; enable Google provider (Google Cloud OAuth client), enable TOTP MFA (the API now REQUIRES `aal2` tokens in prod: `REQUIRE_MFA`; every tester must enrol TOTP, and the `aal` claim shape of real Supabase tokens is the first thing to verify); set Site URL / Redirect URLs after step 9. Collect: project ref, DB password, JWKS URL (or JWT secret), anon key, service-role key. (RUNBOOK 3.2, 4.1)
2. **Secrets**: choose a strong `APP_DB_PASSWORD`; decide the SEC contact and set a **real `SEC_USER_AGENT`** (required by the SEC; the client refuses the placeholder).
3. **Local one-time DB work from your machine** (RUNBOOK 4.2): `python -m app.core.migrate`, `python -m scripts.seed --universe-only` (no demo personas; RUNBOOK 6), `make ingest-xbrl`, `make ingest-filings` (live SEC, first live run on a fresh DB is unverified), run the post-migrate SQL checks.
4. **Meta**: set `META_MODEL_API_KEY`, run `uv run python -m app.modules.llm.smoke` (exit 0 = a live call worked; exit 2 = blocked). Only then `LLM_PROVIDER=meta`. Copy the real price list into `LLM_PRICE_IN/OUT_PER_MTOK`. Read two generated briefs by eye (live prompt/response shapes never ran). Set a spend limit in the Meta console. (RUNBOOK 4.4, 10)
5. **Market data (optional)**: Alpaca key/secret and/or Finnhub, FRED keys; set `MARKET_DATA_SOURCE`; watch `/v1/market/status`. Without keys the app is labelled `simulated` everywhere. (RUNBOOK 4.5)
6. **Render**: create the Blueprint from `render.yaml` on branch `main`, `starter` plan, one instance; fill every `sync: false` variable; deploy; verify `/readyz` 200, `dev-login` 404, `/docs` 404. (RUNBOOK 4.3)
7. **Vercel**: import the web repo; set the four `VITE_*` variables; make `vercel.json` `connect-src` equal the real Render URL; deploy. (web README)
8. **Back-reference**: add the Vercel URL to Render `CORS_ORIGINS` (redeploy) and to Supabase redirect URLs.
9. **DNS / custom domains** if wanted, in the order in RUNBOOK 4.7.
10. **First sign-in** with Google + TOTP; run the beta-user script (RUNBOOK 7); invite testers.
11. **Pull requests** `develop -> main` in both repos (agents never open PRs). Run `make eval` (full), `make test lint`, `npm run e2e` once more on the release commit and commit the refreshed `evals/results/latest.json` first (CR-REL-3).
11b. **Prod start-up guards**: `prod.env` / Render variables must be complete or the API refuses to boot (dev DB password, `postgres:postgres` admin URL, empty `JWT_ISSUER`, placeholder `SEC_USER_AGENT`, `LLM_PROVIDER=meta` without key, Alpaca/Finnhub without keys; RUNBOOK 2). Set `TRUSTED_PROXY_HOPS=1` (render.yaml does) and make sure only one proxy fronts the API.
12. **Legal / regulatory review** before inviting anyone outside the team: SPEC 11.4 frames the product as an educational, paper-trading-only research tool; personalization is the regulatory risk (Lowe v. SEC publisher's-exclusion reasoning, Lingley v. Seeking Alpha). Have counsel review the approved phrasing, the Appendix A disclaimer (shown verbatim at onboarding, footer, briefs, proposals), the "no tailored buy/sell" refusals, and the privacy terms (export/delete in product; DPDP/GDPR timelines are `VERIFY` in SPEC 11.3). This report is not legal advice and no review has taken place.
13. Optional hardening listed under "Known defects" (react-router v7, SEC-01 admin-pool split).

## 5. Unverified functionality (exhaustive, honest)
| Area | What was NOT verified | How to verify |
|---|---|---|
| Supabase Auth | Google OAuth end to end; TOTP MFA enrol/verify; JWKS/HS256 verification against a real project (tested only with locally minted tokens and a mocked JWKS); `DELETE /account` removing the Supabase auth user (mocked); `postgres` role attributes and pgvector schema on Supabase; pooler behaviour with asyncpg | RUNBOOK 3.2, 4.8 |
| Live LLM | Any call to the Meta Model API: endpoint, auth, `reasoning_effort` parameter name, `response_format` support, model ids, India reachability, rate limits, prices. LLM-phrased brief/chat quality; citation *support* (LLM judge), Ragas faithfulness (T-RAG-03b, T-RAG-04b) | RUNBOOK 4.4 |
| Live market data | Alpaca WebSocket + REST and Finnhub against the real services (fakes/mocks only); free-tier limits partly from docs only; pre/post-market labelling; no job refreshes `prices_daily` from Alpaca | RUNBOOK 4.5; `docs/VERIFIED.md` |
| SEC / EDGAR | 429/403 behaviour live; polling `data.sec.gov/submissions` with a real contact on the 60 s alert schedule; ingest on the fresh production DB (done once on the dev DB, 2026-11-01) | RUNBOOK 4.2 |
| Alerts | scheduler under real wall-clock market hours; earnings dates are *estimated* from earlier 8-Ks (no calendar vendor) | run through a market day |
| Docker | `docker build`, container health check, `infra/docker-compose.yml` (never started), CI `docker` job. Substitute: image file layout reproduced and booted without Docker | first CI run / Render build |
| CI | neither workflow has run on GitHub (`.github/workflows/ci.yml` in both repos, gitleaks action, pip-audit in CI, pgvector service, Playwright install) | push `develop` |
| Hosting | Render Blueprint (`render.yaml`) never validated by Render; Render managed Postgres not evaluated (BYPASSRLS requirement); Vercel rewrite/header rules only emulated locally; no real deployment; free-tier limits (`VERIFY` list SPEC 17.1) | RUNBOOK 4.3 |
| Browsers/devices | Chromium only (Playwright). No Firefox, Safari, real phones. Responsive/axe done at 360/390/768/1440 in Chromium | manual pass |
| Load / performance | a Python load smoke ran locally (50 users: dashboard page-load p95 1.24-1.52 s, borderline; 100 users 2.7 s); no k6 run, no brief-generation latency, nothing measured on Render/Supabase hardware | repeat `scripts/load_smoke.py` against the deployed API; k6 |
| Security | SEC-04 MFA gate and SEC-13 tombstones were exercised with locally minted tokens only (real Supabase `aal`/`iat` claims unverified). Prompt-injection scanner and output/input guards are heuristic (PromptGuard 2 and Llama Guard 4 are hooks, not bundled); OWASP ZAP baseline, Semgrep SAST, Dependabot not run; rate limiter behind a real proxy chain, browser XSS/CSP on a deployed site (CSP only smoke-tested locally), MFA/aal and Google OAuth on real Supabase | `docs/security/FINDINGS.md` |
| Personalization quality | bandit/ranking quality measured on synthetic personas only; time-to-personalization see defects; no real-user data | beta |
| Redis / Langfuse / OTel | variables exist, nothing is wired (ADR-004) | out of beta scope |
| Email (daily briefing) | not built (P1) | - |
| Backups | restore rehearsed locally; Supabase PITR/daily backups unverified for the chosen plan | dashboard |
| Secret rotation | procedures written, none exercised | RUNBOOK 12 |

## 6. Known defects and limitations
- **T-PERS-TTP (time-to-personalization <= 30 events)**: the full `make eval` of 2026-11-02 passes at 27 events (the previously committed run failed at 52). Synthetic personas only; treat as indicative until real users exist. The refreshed `latest.json` must be committed.
- **Prices are simulated unless live keys are set, and daily history is always simulated.** The seed writes 120 simulated daily bars per symbol ("through" the seed date, `source=simulated`); nothing refreshes `prices_daily`, so performance/risk history, P/E percentile peers and benchmark curves use simulated history even with live quotes. Every quote carries `source`, `/market/status` has `simulated:true`, and the UI labels it. Paper fills use the quote source.
- **Mock-model prose**: without a Meta key briefs/chat use the extractive `MockProvider` (verbatim 10-K sentences, deterministic, labelled). Numbers are correct (computed in code, verified), prose quality is "ok, not good". The live path is untested.
- **Earnings dates are estimated** (last Item 2.02 8-K + median gap) and say "estimated".
- Brief streaming (SSE) not built; chat citations are not UUIDs, so a source chip navigates to a route instead of opening the drawer (cosmetic).
- Embeddings are lexical hashing (ADR-007), not a semantic model; P/E percentile needs peers' prices (shows `n/m` otherwise); companies that incorporate Items by reference (JPM, XOM, PEP/PG 7A) produce thin sections and "not enough information".
- Seed: `--universe-only` now creates the universe + simulated prices without demo personas (CR-REL-1 closed); the persona-cleanup SQL stays documented for environments that were seeded with them.
- Config guards: closed (SEC-02/18, CR-REL-2). `AUTH_MODE` has no default outside prod (SEC-03).
- `evals.run --fast` no longer overwrites `latest.json` (CR-REL-3 closed); it writes `latest.fast.json`. `make eval` (full) must be re-run on the release commit and its `latest.json` committed (the Docker image serves it at `GET /v1/evals/latest`).
- **Performance**: NFR-01 is borderline at 50 concurrent users on a dev laptop (see section 3); the API is single-process by design.
- **Heuristic safety layers**: the injection scanner, input classifier and output guardrail are regex/normalisation based. They now cover multilingual and obfuscated variants and are measured on 0 / 1,748 real chunks, but a determined paraphrase can pass; PromptGuard 2 / Llama Guard 4 hooks exist and are not bundled. Matters once `LLM_PROVIDER=meta`. The output guard also still blocks "A strong buy rating does not guarantee ..." (negation is only looked up backwards).
- **Open security items** (none medium or above): SEC-01 admin pool in the API process, SEC-05 react-router, SEC-16 tokens in localStorage, SEC-22 SECURITY DEFINER helpers.
- `.env.example` once documented the admin DB URL as optional at runtime; it is required (order sweeper). Fixed in docs (CR-REL-4).
- Single instance by design (in-process quote store, SSE, scheduler, rate limiter): no horizontal scaling, a restart drops SSE connections (clients reconnect with Last-Event-ID).
- Rate limits are in-memory per instance; defaults were changed during hardening to IP 400/40 per s, user 300/30 per s.
- Web: lightweight-charts injects two `<style>` elements allowed by CSP hash (breaks on library upgrade until hashes are updated; the smoke test shows the new ones); the AI glyph needs `style-src-attr 'unsafe-inline'`; `react-router-dom` 6.x has 2 moderate advisories (open redirect via backslash in `<Link>`, SSR hydration; fix = v7 major); a stale lazy chunk after a redeploy yields a load error until refresh; magic-link sign-in not built.
- `npm run format:check` fails repo-wide on pre-existing Markdown (`.claude/agents/*.md`, `CLAUDE.md`); CI formats `src e2e scripts` only.
- Seed data: 16 design tickers + V + SPY = 17 in-universe, SPEC asks 15-20 (met).

## 7. Files delivered by the release work
Backend (BE-22 additions: `app/core/textnorm.py`, `app/core/cursors.py`, `migrations/0900_account_tombstones.sql`, `scripts/load_smoke.py`, `tests/test_config_hardening.py`, `tests/safety/test_hardening.py`): `Dockerfile` (fixed: evals results copied, `$PORT`, HEALTHCHECK), `.dockerignore`, `infra/docker-compose.yml`, `render.yaml`, `.github/workflows/ci.yml` (adds gitleaks, docker job, concurrency), `.gitleaks.toml`, `.env.example` (admin URL, JWT leeway/JWKS TTL, PORT, rate-limit defaults), `Makefile` (`prod-check`, `docker-build`, `compose-up`), `scripts/prod_start_check.py`, `docs/RUNBOOK.md`, `docs/RELEASE.md`.
Web: `vercel.json`, `.env.production.example`, `vite.config.ts` (no source maps in production; `BUILD_SOURCEMAP=hidden` opt-in), `scripts/{check_bundle_size,check_deploy,serve_dist}.mjs`, `scripts/e2e_deploy.sh`, `playwright.deploy.config.ts`, `e2e/deploy-smoke.spec.ts`, `.github/workflows/ci.yml`, README deploy section, `docs/RELEASE.md`, STATUS/TASKS/VERIFICATION.
