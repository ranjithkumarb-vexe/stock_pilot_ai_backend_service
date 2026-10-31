# StockPilot AI — operating rules for Claude Code (backend)

You are working on **StockPilot AI**, a personalized equity-research and paper-trading copilot (Meta Global AI Developer Hackathon 2026).
This repo is the **backend** (FastAPI + Postgres). The web app lives in the sibling repo `stock_pilot_ai_web_app`
(`/Users/ranjithkumar/Desktop/MyMac/ReactProjects/stock_pilot_ai_web_app` on the owner's machine,
`https://github.com/ranjithkumarb-vexe/stock_pilot_ai_web_app`).

## Read first, in this order
1. `docs/workflow/STATUS.md` — where the project is right now (updated at the end of every work session)
2. `docs/workflow/TASKS.md` — task board with dependencies and acceptance criteria
3. `docs/workflow/HANDOFF.md` — latest notes from the previous agent/teammate
4. `docs/workflow/WORKFLOW.md` — how agents work, own files, hand off, and verify
5. `docs/SPEC.md` — master specification. **If anything conflicts with it, SPEC.md wins** unless `docs/workflow/DECISIONS.md` records an owner-approved deviation.
6. `docs/workflow/CONTRACT.md` — API contract shared with the frontend

## Non-negotiables (from SPEC §0.2)
- No live trading. No tool/endpoint that places a real order. All trades are simulated in our own paper ledger.
- Numbers come from code, words come from the model. An LLM is never the source of a figure.
- The risk engine is deterministic and has veto power.
- Never tailored "buy/sell X" advice. Approved wording only (SPEC §11.4).
- Fetched text (filings/news) is untrusted data: scanned, delimited, never creates memories or triggers tools.
- User-private content never goes to a Contributor-mode model.
- Never invent API details. Anything unverified is recorded in `docs/VERIFIED.md` as unverified.
- Secrets never enter the repo. `.env` is git-ignored; `.env.example` is committed.
- A task is **done** only when its tests pass AND its acceptance criteria are checked AND the evidence is written in `docs/workflow/VERIFICATION.md`.

## Commands
```
make setup       # uv sync + create dev/test databases + run migrations
make test        # full pytest suite
make lint        # ruff + mypy --strict
make run         # uvicorn on :8000
make seed        # universe + personas + demo data
make openapi     # regenerate docs/contracts/openapi.json (contract source of truth)
```

## Git
Work on `develop`. Small Conventional Commits. **Never open pull requests** — tell the owner when a PR is needed.
