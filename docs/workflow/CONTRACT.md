# API contract (human-readable overview)

Source of truth for exact schemas = backend OpenAPI (`docs/contracts/openapi.json`, regenerate with `make openapi`).
This file fixes **conventions** and the **resource shapes the UI needs**, so FE and BE agents can build in parallel.
Where this file and the OpenAPI differ, fix the one that is wrong in the same change.

## Conventions
- Base path `/v1`. JSON, **snake_case** keys. Timestamps ISO-8601 UTC (`2026-10-31T14:05:00Z`). Dates `YYYY-MM-DD`.
- Money/prices: JSON numbers (USD), 2 dp for money, up to 4 dp for prices; backend uses `Decimal`. Percentages are percent numbers (`6.8` = 6.8%), field names end in `_pct`. Weights are percent numbers too (`weight_pct`).
- Tickers are upper-case strings; all other ids are UUID strings.
- Auth: `Authorization: Bearer <jwt>` on everything except `/healthz`, `/readyz`, `/v1/auth/*`, `/v1/onboarding/questions`, `/v1/universe`.
  - `AUTH_MODE=supabase`: verify Supabase JWT (HS256 secret `SUPABASE_JWT_SECRET`, or JWKS URL), `sub` = user id, `email` claim.
  - `AUTH_MODE=dev`: `POST /v1/auth/dev-login {persona: "priya"|"alex"|"sam"|"new"}` → `{access_token, expires_in, user}`; same claims, signed with `DEV_JWT_SECRET`. Route returns 404 when AUTH_MODE != dev.
  - SSE cannot send headers: `POST /v1/stream/token` → `{token, expires_in: 60}`; `GET /v1/stream?token=...`.
- Errors: HTTP status + body `{code, message, trace_id, details?}`. Codes: `unauthenticated`(401) `forbidden`(403) `not_found`(404) `conflict`(409) `validation_error`(422) `rate_limited`(429, `Retry-After`) `rule_violation`(422, `details.violations[]`) `stale_quote`(409) `market_closed`(409) `insufficient_cash`(422) `insufficient_shares`(422) `unsupported_symbol`(422) `guardrail_blocked`(422) `upstream_unavailable`(503) `internal`(500) `method_not_allowed`(405) `payload_too_large`(413). `rate_limited` also carries `details.retry_after_s`. `DELETE /account` returns 204 with no body.
- Every response carries header `X-Trace-Id` (same as `trace_id`).
- Idempotency: `POST /v1/orders` requires `Idempotency-Key` header (UUID). Replays return the original response with `Idempotent-Replay: true`.
- Pagination: `?limit=` (default 50, max 200) & `?cursor=`; list responses `{items: [...], next_cursor: string|null}`. Small bounded lists (watchlist, positions) return plain `{items}`.
- Disclaimer: briefs and proposals carry `disclaimer` (Appendix A verbatim).

## Endpoints (all under `/v1`)
| Area | Method & path | Notes |
|---|---|---|
| Ops | `GET /healthz` `GET /readyz` | unauthenticated; readyz checks DB |
| Auth | `POST /auth/dev-login` | dev only |
| Session | `GET /me` | `{user_id, email, display_name, initials, onboarded, persona_id|null}` |
| Onboarding | `GET /onboarding/questions` · `POST /onboarding` | questions: `{questions:[{id,text,options:[{label,score}]}], tiers:[{tier,label}], horizons[], experience_levels[], sectors[]}`; POST body `{answers:[6 ints], horizon, goals[], experience, likes[], excludes[], esg_exclusions[], watchlist[], region, currency, starting_cash, disclaimer_ack: true}` → `{profile, risk_tier, tier_label, explanation, rules, watchlist}` (409 if already onboarded) |
| Profile | `GET /profile` · `PATCH /profile` · `PUT /profile/rules` · `POST /profile/reset` · `GET /profile/export` · `DELETE /profile/memories/{id}` · `DELETE /account` (body `{confirm:"DELETE"}`) | GET returns `{profile, style_card, rules, interests:[{dim,weight,baseline}], memories:[{id,kind,text,confidence,source}], bias:[{name,metric,value,state:"ok"|"watch"|"flag"}], stated_tier, revealed_tier, gap_days, recent_events:[{id,ts,type,text}]}` |
| Universe | `GET /universe` | `{items:[{symbol,name,sector,industry,exchange}]}` |
| Watchlist | `GET /watchlist` · `PUT /watchlist/{symbol}` · `DELETE /watchlist/{symbol}` | items include `quote` |
| Quotes | `GET /quotes?symbols=` · `GET /market/status` · `POST /stream/token` · `GET /stream` (SSE) | quote: `{symbol, price, prev_close, change_pct, bid?, ask?, ts, source, session:"pre"|"regular"|"post"|"closed", freshness:"live"|"delayed"|"stale"|"closed"}`; SSE events `quote`,`alert`,`proposal`,`heartbeat` (every 15 s, `id:` for Last-Event-ID) |
| Dev market controls | `POST /dev/market {open: bool}` · `POST /dev/feed {state:"live"|"delayed"|"stale"|"reconnecting"}` | only when `AUTH_MODE=dev`; drive the "simulate" controls in the UI |
| Chart | `GET /quotes/{symbol}/bars?interval=5m|1d&limit=` | `{symbol, interval, bars:[{time,open,high,low,close,volume}], source}` |
| Briefs | `GET /briefs/{symbol}` · `POST /briefs/{symbol}/generate` | see Brief shape; `GET /citations/{id}` |
| Alerts | `GET /alerts?type=&state=` · `POST /alerts/{id}/action` `{action: open|dismiss|snooze}` | alert: `{id, type: breach|filing|move|earnings|mirror|concentration, symbol|null, critical, materiality, title, body, source_label, state, created_at, why}` |
| Proposals | `GET /proposals?status=` · `GET /proposals/{id}` · `POST /proposals/{id}/decision` | proposal: `{id, kind: trade|review, symbol|null, side|null, rule|null, title, body, rationale[], rules_checked[], suggested_qty, current_weight_pct, after_weight_pct, cap_pct, sources[], status, expires_at, created_at, disclaimer, why}`; decision body `{decision: approve|reject|modify, reason?: too_risky|disagree|timing|already_know, qty?}` → `{proposal, order|null}` |
| Portfolio | `GET /portfolio` · `GET /portfolio/risk` · `GET /portfolio/performance` | portfolio: `{portfolio_id, cash, invested, equity, day_change, day_change_pct, positions:[{symbol,name,sector,qty,avg_cost,price,change_pct,value,weight_pct,pnl,pnl_pct}], sectors:[{sector,value,weight_pct}], hhi, effective_n}`; risk: `{metrics:{hhi,effective_n,vol_60d,beta,max_drawdown_pct,drawdown_pct,trades_7d}, rules:[{key,limit,current,status:"ok"|"breach"|"warn"}], violations[]}`; performance: `{series:[{ts,equity,spy_norm}], stats:{ytd_pct,spy_pct,vol_pct,beta,drawdown_pct,max_drawdown_pct,turnover_pct,win_rate_pct,realized_pnl}}` |
| Orders | `POST /orders/preview` · `POST /orders` · `GET /orders` · `DELETE /orders/{id}` | preview/create body `{symbol, side, type: market|limit, qty, limit_price?, proposal_id?, override?: bool}`; response `{order, fill|null, checks:[{key,ok,severity:"pass"|"note"|"blocked",label,detail}], blocked}`; order status `queued|working|filled|cancelled|expired|rejected` |
| Feed | `GET /feed` · `GET /why/{output_type}/{id}` | feed item `{kind: alert|proposal|briefing, id, pinned, score, item, why}`; why: `{summary, top_features:[{name,weight,detail}], rules_checked:[{rule,passed}], sources[], basis:"profile"|"behaviour"|"cold_start"}` — built from `personalization_audit`, never LLM |
| Events | `POST /events` | `{events:[{event_id,ts,session_id,type,entity,props,context,client,schema_version}]}` → `{accepted, rejected:[{index,reason}]}` (max 100/batch) |
| Chat | `POST /chat` · `GET /chat/history` | request `{message, thread_id?}` → `{id, thread_id, answer, citations[], refused, refusal_reason|null, trace_id}`; refusal for advice/guarantee asks |
| Audit | `GET /audit/traces` · `GET /audit/traces/{id}` · `GET /evals/latest` | trace: `{id, name, model, mode, duration_ms, tokens_in, tokens_out, cost_usd, outcome:"ok"|"blocked"|"error", steps:[{name,ms,outcome,detail}]}`; evals: `{generated_at, thresholds_met, items:[{id,description,target,actual,status:"pass"|"fail"}]}` |

## Brief shape
```
{ id, symbol, name, as_of, generated_at, status: "ok"|"degraded", stale: bool, stale_reason|null,
  style_card, section_order: ["bal","risk",...],
  sections: [{ key: trend|bal|val|moat|risk|cat|bb, title, source_label,
               claims: [{ text /* final rendered text, numbers substituted by code */, citation_ids[], ledger_refs[] }],
               rows: [{ label, value, source }], plain_hint|null }],
  bull: {text, citation_ids[]}, bear: {text, citation_ids[]},
  ledger: { m1: {label, value, display, unit, source} }, citations_summary: {coverage_pct, unverified_numbers: 0},
  disclaimer, trace_id }
```
Citation (`GET /citations/{id}`): `{id, kind:"chunk"|"xbrl"|"price", title, company, doc_label, accession_no, filed_at, fetched_at, sha256, url, excerpt, rows:[{label,value,source}]}`.

## Rules object
`{pos, sec, dd, trades, cash}` — percentages for pos/sec/dd/cash, integer for trades (per rolling 7 days).

## Change log
| Date | Change | By |
|---|---|---|
| 2026-10-31 | Initial contract drafted from SPEC §9 and the finalized UI | orchestrator |
| 2026-10-31 | Added error codes method_not_allowed, payload_too_large; rate_limited details; DELETE /account 204 (CR-1, CR-2) | orchestrator |
