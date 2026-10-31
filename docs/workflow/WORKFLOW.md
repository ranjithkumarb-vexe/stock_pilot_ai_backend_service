# Multi-agent workflow

This is the operating manual for the Claude agents that build, integrate, test and release StockPilot AI.
It lives in **both** repos (the backend copy is canonical; run `scripts/sync_workflow.sh` to refresh the frontend copy).
A teammate (or a fresh Claude session) can resume from the repo alone: read `STATUS.md`, `TASKS.md`, `HANDOFF.md`.

## 1. Agents
Definitions are in `.claude/agents/*.md` (same in both repos). Each has a fixed responsibility and file ownership.

| Agent | Responsibility | Owns |
|---|---|---|
| `orchestrator` | Plans waves, creates/updates tasks, dispatches agents, resolves conflicts, commits, keeps STATUS/HANDOFF current | `docs/workflow/*` |
| `architect` | Architecture, ADRs, API contract design, cross-cutting rules | `docs/ADR/*`, `docs/workflow/CONTRACT.md`, `docs/workflow/DECISIONS.md` |
| `database-engineer` | Schema, migrations, RLS policies and RLS tests, seed data | `migrations/*`, `scripts/seed*`, `tests/db/*` |
| `backend-engineer` | FastAPI modules, domain engines, integrations | `app/modules/<assigned>/*`, `tests/<assigned>/*` |
| `frontend-engineer` | React screens/components ported from the finalized design | `src/features/<assigned>/*` |
| `integration-engineer` | Keeps FE and BE contract-consistent; wires real API into UI; cross-repo E2E | `docs/contracts/*`, `src/api/*` (FE), `e2e/*` |
| `qa-engineer` | Test strategy, regression/E2E/a11y/responsive/error scenarios, bug triage | `tests/*`, `e2e/*`, `docs/workflow/VERIFICATION.md` |
| `security-reviewer` | Auth, RLS, injection, SSRF, secrets, OWASP LLM controls, dependency audit | read-only + findings in `docs/security/*` |
| `release-manager` | Deploy config, env templates, runbook, release readiness report | `Dockerfile`, `infra/*`, `docs/RUNBOOK.md`, `docs/RELEASE.md` |

## 2. Lifecycle of one task
1. **Pick** a task from `TASKS.md` whose dependencies are all `done`. Set it `in_progress` with your agent name.
2. **Plan** in 3–6 lines in the task's notes (files you will touch; contract impact).
3. **Test first** where practical: write the failing test that encodes the acceptance criteria.
4. **Implement** only inside the files you own. If you need a change in a file you don't own, write a *Contract/Change Request* (section 4) instead of editing.
5. **Verify**: run the task's test command, `make lint`, and the relevant manual/API check. Fix failures; repeat.
6. **Record**: add a row to `VERIFICATION.md` (command, result, date). Update task status to `done` only if every acceptance bullet is evidenced. Otherwise `blocked` or `partial` with the reason.
7. **Hand off**: append to `HANDOFF.md` (what changed, how to run, what is next, gotchas).

## 3. Status vocabulary
`todo` · `in_progress` · `blocked` (needs credential/decision/other task) · `partial` (works, some acceptance unverified — list which) · `done` (all acceptance evidenced) · `deferred` (out of beta scope, with reason).
Never mark `done` on the basis of "it should work". Unverified things are listed in `STATUS.md → Unverified / needs owner`.

## 4. Coordination between frontend and backend
- **The backend OpenAPI document is the contract.** `make openapi` writes `docs/contracts/openapi.json`; the frontend generates types from a copy at `contracts/openapi.json` (`npm run gen:api`). CI fails if the copy drifts (`scripts/check_contract.sh`).
- `docs/workflow/CONTRACT.md` is the human-readable contract (conventions, error codes, resource shapes). Changing a response shape requires: (1) change in CONTRACT.md, (2) backend change + test, (3) openapi regen, (4) frontend sync + typecheck. One agent may do all four if it owns both; otherwise file a **Change Request** in `HANDOFF.md` under `## Open change requests` using the template:
  `CR-<n> | from: <agent/repo> | to: <agent/repo> | what/why | blocking task ids | status`
- Validation rules live in the backend (Pydantic). The frontend mirrors only UX-level checks, and always surfaces backend error `message` + `code`.
- Auth: Supabase JWT in production, built-in dev tokens when `AUTH_MODE=dev`. Same claim shape (`sub`, `email`). Details in CONTRACT.md §Auth.
- Error shape is always `{code, message, trace_id, details?}`. Frontend maps `code` → UX copy in one file (`src/api/errors.ts`).

## 5. File ownership & parallel work
Parallel agents must have disjoint file sets (see "Owns"). Modules are auto-discovered (`app/modules/<name>/router.py` exposing `router`), so no agent edits `app/main.py`. Migrations use reserved number blocks per module (see TASKS.md → *Migration blocks*). Parallel agents use their own test database (`SP_TEST_DB=stockpilot_test_<agent>`) and their own dev ports (see each task). Agents do **not** commit; the orchestrator commits after verifying (so history stays clean and one agent's red state never lands).

## 6. Release gate (what "ready for beta" means)
All of: every P0 task `done` or explicitly `deferred` with owner-visible reason; backend `make test` green; frontend `npm run test && npm run build` green; cross-repo E2E green; no high/critical findings open in `docs/security/`; migrations apply cleanly on an empty database; `.env.example` complete; `docs/RUNBOOK.md` verified step by step; `docs/RELEASE.md` lists remaining blockers/credentials/unverified items honestly.

## 7. Resuming
Fresh session: `claude` in the repo, then say "resume the StockPilot workflow". The `orchestrator` agent reads STATUS → TASKS → HANDOFF, picks the next unblocked wave, and dispatches the other agents.
