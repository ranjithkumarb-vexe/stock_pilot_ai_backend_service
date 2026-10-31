# Project status — backend

_Last updated: 2026-10-31 by orchestrator._

## Summary
Workflow scaffolding committed. Wave 1 (foundation) in progress. Nothing is production-ready yet.

## Gate table
| Gate | State |
|---|---|
| Foundation (scaffold, schema, auth) | not started |
| Domain features | not started |
| Cross-repo E2E | not started |
| Security review | not started |
| Release readiness | not started |

## Unverified / needs owner
- Meta Model API key & India access (SPEC §17.1) — not provided; LLM runs in degraded/mock mode until `META_MODEL_API_KEY` is set.
- Alpaca / Finnhub / FRED keys — not provided; market data runs on a clearly-labelled simulated source.
- Supabase project (Auth, Google OAuth, MFA) — not provided; `AUTH_MODE=dev` used locally.
- Docker is not installed on the dev machine — compose/Docker build unverified.

## Sibling repo
Frontend: https://github.com/ranjithkumarb-vexe/stock_pilot_ai_web_app (local: /Users/ranjithkumar/Desktop/MyMac/ReactProjects/stock_pilot_ai_web_app)
