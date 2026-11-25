# Launch blockers (beta rollout) — assessed 2026-11-25

**Verdict: NOT READY to roll out.** The code is feature-complete and tested (backend 1,583 tests, web 280 + 38 live E2E, security review: 0 critical/high/medium open), but it has never run on real infrastructure, with a real login, a real LLM or real market data, and `main` still holds only the initial commit in both repos.

Legend — **Owner**: only the product owner can do it (accounts, money, credentials, legal). **Agent**: engineering work the workflow agents can finish now. **Both**: agents prepare, owner approves/executes.
IDs are tracked in both repos' `docs/workflow/TASKS.md` as `LB-nn`.

## A. Owner-gated (cannot be closed by agents)
| ID | Blocker | Why it blocks | What is needed |
|---|---|---|---|
| LB-01 | **`develop` → `main` PRs not merged** (main = initial commit in both repos). Backend first, then web | Render/Vercel deploy from `main`; CI has never run on GitHub | Raise + merge both PRs |
| LB-02 | **GitHub CLI login expired** (HTTP 401) | Cannot read CI results or open PRs from this machine | `gh auth login -h github.com` |
| LB-03 | **No Supabase project for StockPilot** (the only project in the account, "Credit Management SaaS", is unrelated and must not be used) | No real auth/DB: Google OAuth, TOTP MFA (`REQUIRE_MFA` demands `aal2` in prod), JWKS, pgvector, `postgres` role/BYPASSRLS are all unverified | Create project; Google OAuth client; enable TOTP; give project ref + keys (secrets stay in env/Render, never in chat/repo) |
| LB-04 | **No Meta Model API key** | Live LLM path never ran: endpoint, params, structured output, India reachability, prices, brief/chat prose quality, T-RAG-03b/04b evals | `META_MODEL_API_KEY`; run `python -m app.modules.llm.smoke`; set console spend limit |
| LB-05 | **No market-data keys** (Alpaca, Finnhub, FRED) | Prices are labelled `simulated`; IEX free tier is non-commercial | Keys + choose `MARKET_DATA_SOURCE`; licensing decision for commercial use |
| LB-06 | **Real `SEC_USER_AGENT` contact** | SEC fair-access; prod refuses the placeholder | A monitored contact email |
| LB-07 | **Hosting not provisioned** (Render API, Vercel web, domains, CORS/redirect URLs, secrets) | Nothing deployed | Approve cost + accounts; deploy per `docs/RUNBOOK.md` |
| LB-08 | **Legal/regulatory review** (SPEC §11.4; US publisher's-exclusion, SEBI, MiFID; privacy/DPDP/GDPR) | Required before ANY external user; Terms of Service + Privacy Policy text must come from counsel | Counsel review + final legal copy |
| LB-09 | **Beta operations decisions**: invite list, support contact, incident owner, error-alert channel, backup plan (Supabase PITR tier) | No one is on point if it breaks | Decide + name owners |
| LB-10 | **Production secrets rotation/ownership** (DB password, JWT issuer, keys) | Guards refuse to boot without them | Generate/store in Render/Vercel/Supabase secret stores |

## B. Engineering (agents can close now)
| ID | P | Item | Evidence it's open | Acceptance |
|---|---|---|---|---|
| LB-11 | P0 | **Daily price history is always simulated**; nothing refreshes `prices_daily`; no scheduled data refresh for new 10-K/10-Q/8-K facts | RELEASE §6 | Scheduled jobs: daily bars (Alpaca REST → `prices_daily`, split-adjusted, SPY incl.), XBRL/filings refresh; idempotent; admin-pool only; tested with fakes; `source` column honest; live run = unverified until keys (LB-05) |
| LB-12 | P0 | **Earnings dates are estimates** from old 8-Ks | RELEASE §6 | Finnhub earnings-calendar adapter (fakes), falls back to the estimate and still says "estimated"; unverified live until LB-05 |
| LB-13 | P0 | **QA-D1: interest-drift prompt never reaches the user** | `tests/qa` strict xfail | `GET /v1/profile` exposes `drift`; dashboard card; dismiss endpoint; xfail removed |
| LB-14 | P0 | **Performance: p95 1.24–1.52 s at 50 users (budget 1.5 s), 2.7 s at 100**; dashboard makes ~25 calls per load | RELEASE §3 | p95 ≤ 1.0 s at 50 users on the dev laptop, no errors at 100; automated T-PERF-01 in evals; hot-query/index/cache/pool fixes with tests |
| LB-15 | P0 | **Legal pages missing in the app** (Terms, Privacy, Disclaimer page, footer links, onboarding links) | no route exists | Pages with placeholder text clearly marked "pending counsel review" (final text = LB-08); routes public; axe clean |
| LB-16 | P0 | **No error monitoring/uptime hooks** | Langfuse/OTel unwired (ADR-004) | Optional Sentry (backend + web) and structured request/job metrics endpoint; no-op without DSN; PII scrubbed; documented alert rules |
| LB-17 | P1 | **react-router 6 advisories** (SEC-05) | `npm audit` 2 moderate | Upgrade to v7 (library mode), e2e 38/38, `npm audit --omit=dev` clean |
| LB-18 | P1 | **Chromium-only testing** | RELEASE §5 | Playwright Firefox + WebKit pass the E2E + deploy-smoke suites (fix defects) |
| LB-19 | P1 | **No dependency/SAST automation** (Dependabot, Semgrep, CodeQL, ZAP baseline) | RELEASE §5 | `.github/dependabot.yml`, Semgrep/CodeQL workflows in both repos; local Semgrep run clean |
| LB-20 | P1 | **SEC-01: BYPASSRLS admin pool inside the API process** | FINDINGS SEC-01 | Narrow SECURITY DEFINER functions or a separate worker entrypoint; API runs on the app role only; or explicit accepted-risk ADR with mitigation |
| LB-21 | P1 | T-DATA-04 display-timezone has no web test; chat xbrl/portfolio citations have no drawer; brief streaming and magic link not built | QA matrix | Tests/fixes for TZ; others recorded as `deferred` with reason |
| LB-22 | P0 | **Docs/runbook must match reality after the above**, `RELEASE.md` regenerated, fresh full verification | — | `make test lint prod-check eval`, web gates, E2E (Chromium + Firefox + WebKit), deploy smoke all re-run and recorded |

## C. Will remain unverified until the owner items land
Supabase Google/TOTP/aal/iat/JWKS; live Meta calls; live Alpaca/Finnhub; first live ingest on the production DB; Render Blueprint; Vercel headers; Docker build; GitHub Actions; load on real hardware; real-user personalization quality.

## D. What "ready for beta" will mean
All B items `done` or `deferred` with reason; all A items executed by the owner and re-verified by the agents against the real services using `docs/RUNBOOK.md` §7 (beta-user script); `RELEASE.md` status line changed to `READY FOR BETA`.
