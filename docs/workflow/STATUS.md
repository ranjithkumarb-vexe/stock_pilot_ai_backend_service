# Project status — backend

_Last updated: 2026-11-02 by the BE-22 hardening agent (after REL-01)._

## Summary
Tasks (25): **18 done, 6 partial, 1 todo**. Partial: BE-09, BE-13, BE-14, BE-16, BE-17, SEC-01. Todo: QA-01 (SPEC 12.4 matrix). Release status in `docs/RELEASE.md`: **NOT READY by the written gate** because gate 1 (QA-01 todo; five tasks partial pending live services/owner decisions) and gate 8 (RUNBOOK hosted parts unexecuted) are not met; every engineering blocker found so far is closed. BE-22 (hardening) is done: full pytest **1571 passed, 0 xfailed, 95% coverage**, ruff/format/mypy clean, CR-REL-1..5 closed, **0 medium security findings open** (SEC-02/03/04/08/11/12 + 06/07/10/13/17/18/20 fixed with tests), full `make eval` **38 pass / 0 fail / 4 not measured**, web `npm run e2e` **38/38**, `make prod-check` 19/19. New honest findings: dashboard load smoke is borderline (p95 1.24-1.52 s at 50 users vs 1.5 s budget; 2.7 s at 100 users); MFA (`aal2`) enforcement and account tombstones are verified with local tokens only. Nothing is deployed; Supabase, Render, Vercel, Docker, GitHub Actions, live LLM and live market data have never run.

## Gate table (WORKFLOW section 6; evidence in `docs/RELEASE.md`)
| Gate | State |
|---|---|
| P0 tasks done/deferred | **not met** (QA-01 todo; 6 partial: owner must verify or defer) |
| Backend `make test` | **met** (1571 passed, 0 xfailed, 95%; ruff + mypy clean) |
| Frontend test + build | met (280 vitest, lint, typecheck; build/budgets at INT-03) |
| Cross-repo E2E | **met** (38/38 re-run 2026-11-02 after the hardening) |
| Security: no high/critical | met (0 critical, 0 high, 0 medium open) |
| Migrations on empty DB | met (18 migrations incl. 0900, executed) |
| `.env.example` complete | met |
| RUNBOOK verified | partial (local parts executed; hosted parts not) |
| RELEASE honest | met |

## Unverified / needs owner
- Supabase project (Google OAuth, TOTP MFA and the real `aal` claim the API now enforces, `postgres` role BYPASSRLS, pgvector schema, pooler) — nothing run against a real project.
- Meta Model API key & live call (`python -m app.modules.llm.smoke`), real LLM prices; Alpaca / Finnhub / FRED keys; a real `SEC_USER_AGENT` contact (prod now refuses the placeholder) and the first live ingest on the production DB.
- Docker build/compose, Render Blueprint, Vercel deploy, GitHub Actions (both workflows; the docker job was adapted to the new prod guards but never run) — never run (no Docker/accounts here).
- Load on real hardware (NFR-01 borderline locally), brief-generation latency, k6; cross-browser; legal/regulatory review (SPEC 11.4); ZAP/Semgrep baselines.
- QA-01: the SPEC 12.4 scenario-to-test matrix has not been written (the eval scoreboard covers most T-* ids).
- Full list: `docs/RELEASE.md` section 5; owner checklist in section 4; procedures in `docs/RUNBOOK.md`.

## Sibling repo
Frontend: https://github.com/ranjithkumarb-vexe/stock_pilot_ai_web_app (local: /Users/ranjithkumar/Desktop/MyMac/ReactProjects/stock_pilot_ai_web_app)
