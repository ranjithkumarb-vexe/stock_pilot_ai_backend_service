# Decisions log (ADR index)

| ID | Date | Decision | Why | Deviates from SPEC? |
|---|---|---|---|---|
| ADR-001 | 2026-10-31 | Frontend is **Vite + React 18 + TypeScript**, not Next.js | Owner's brief says "React JS"; no SSR needed for an authenticated dashboard; simpler static hosting on Vercel | Yes (SPEC §7.1 says Next.js) — owner instruction wins |
| ADR-002 | 2026-10-31 | Backend talks to **plain Postgres 15+ with pgvector**; Supabase is a deploy target (Auth + managed Postgres), not a local requirement | No Docker/Supabase locally; keeps dev loop fast; RLS via `app.user_id` setting works on both | Partly (SPEC says Supabase) |
| ADR-003 | 2026-10-31 | `AUTH_MODE=dev|supabase` with identical JWT claims | Lets everything be tested without a Supabase project | No |
| ADR-004 | 2026-10-31 | In-process quote store, scheduler (APScheduler) and background tasks; Redis optional | One API instance is enough for beta; fewer moving parts | Partly (SPEC lists Redis+Arq) |
| ADR-005 | 2026-10-31 | LLM via provider interface with `MockProvider`/degraded template mode when no key; local hashing embeddings until an embedding model is verified | No Meta key available; numbers never depend on the LLM anyway (SPEC §0.2.2) | No |
| ADR-006 | 2026-10-31 | Work on `develop` in both repos; no PRs raised by agents | Owner request | n/a |
