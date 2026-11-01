# Verified vs unverified external details

Rule (CLAUDE.md): never invent API details. Everything below marked **UNVERIFIED** was implemented from the
SPEC/OpenAI-compatible convention and is exercised only against mocks. Status as of 2026-11-01 (stream B).

## SPEC 17.1 "verify-today" list
| # | Item | Status | What the code assumes / what to do |
|---|---|---|---|
| 1 | Claim the $150 Meta API credits; card requirement; spend alert | UNVERIFIED (owner action) | Nothing in code. Budgets: `LLM_BUDGET_ALERT_USD=100`, `LLM_BUDGET_HARD_STOP_USD=140`, `LLM_USER_DAILY_BUDGET_USD` (default 1.00) are enforced from `llm_calls` rows. |
| 2 | Meta Model API reachable from India | UNVERIFIED / **blocked: no `META_MODEL_API_KEY`** | `MetaProvider` is implemented and unit-tested against a mocked transport only. First live call = `LLM_PROVIDER=meta META_MODEL_API_KEY=... uv run python -m app.modules.llm.smoke` (exit 2 = blocked, 0 = ok, 1 = degraded). |
| 3 | Official rules / judging details | UNVERIFIED (owner action) | n/a to the backend. |
| 4 | Alpaca free tier limits | stream C | not stream B. |
| 5 | Free-tier hosting limits | UNVERIFIED | n/a to stream B. |
| 6 | LlamaFirewall / PromptGuard 2 / Llama Guard 4 packaging | UNVERIFIED | Not bundled. Hook interfaces exist: `research.injection.register_classifier()` (PromptGuard 2 style `score(text)->float`) and `safety.InputClassifier(hook)` (Llama Guard 4 style `is_unsafe(text)->bool`). The shipped baseline is regex heuristics (25/25 on the injection set, 0 false positives on 1,748 real filing chunks). |
| 7 | Embedding model available via Meta API / open-weight | UNVERIFIED | Using deterministic local hashing embeddings, 1024-d (ADR-007). `doc_chunks.embedding vector(1024)` unchanged when a real model is chosen. |
| 8 | Finnhub / NewsAPI / FRED terms | stream C | not stream B. |

## Meta Model API details assumed by `app/modules/llm/providers/meta.py` (all UNVERIFIED)
- Endpoint `POST {META_API_BASE_URL}/chat/completions` (default base `https://api.meta.ai/v1`), `Authorization: Bearer <key>`.
- Request keys: `model`, `messages[{role,content}]`, `max_tokens`, `temperature`, `reasoning_effort`.
  The parameter *name* (`reasoning_effort` vs a nested `reasoning: {effort}`) is not confirmed. SPEC 7.3 says effort `none`
  is HTTP 400, so the client only ever sends `minimal|low|medium|high`.
- Structured output as `response_format: {type: "json_schema", json_schema: {name, schema, strict: true}}`: support and
  strictness semantics not confirmed. The client validates the returned JSON itself with Pydantic and retries once, so a
  provider that ignores `response_format` still works.
- Response: `choices[0].message.content`, `usage.prompt_tokens/completion_tokens`, top-level `model`.
- Retry semantics: 408/409/425/429/5xx and network errors are retried with exponential backoff + jitter; `Retry-After` (seconds)
  honoured; 400/401/403/404 are not retried.
- Model ids `muse-spark-1.3`, `muse-spark-1.3-contributor`, `muse-wonder-1.0` come from env (`MODEL_*`) and are taken from SPEC 6.3; unconfirmed.
- Contributor mode ~150 RPM limit (SPEC 7.3) is not enforced client-side; only retry/backoff handles 429.
- Prompt caching, search grounding: not used.
- Pricing: `LLM_PRICE_IN_PER_MTOK=1.00`, `LLM_PRICE_OUT_PER_MTOK=4.00` are **placeholders**; `llm_calls.cost_usd` and therefore the
  $100/$140 alerts are only as accurate as these numbers. Set the real price list before relying on the budget stop.

## SEC EDGAR (verified live on 2026-11-01)
- `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` and `/submissions/CIK##########.json`, Archive documents on `www.sec.gov`: all reachable
  with `User-Agent: StockPilot remoranjith019@gmail.com`; 16/16 tickers ingested, 79 filing documents fetched with <= 8 req/s.
- 429/403 behaviour was **not observed live** (no throttling occurred); it is covered by mocked-transport tests only.
- Companies that incorporate Item 7 by reference to an exhibit (JPM, XOM; PEP/PG for Item 7A) yield thin/absent sections in the main 10-K
  document; the ingest reports them as `thin_sections` and briefs say "not enough information" instead of guessing.

## Other
- `LLM_PROVIDER=mock` outputs are deterministic extractive text, never model output; briefs generated this way say nothing about model quality.
- LLM-judge metrics (citation *support* >= 90%, faithfulness >= 0.90, hallucination <= 2%; T-RAG-03 support half, T-RAG-04) need a live judge
  model: **not measured**. Only citation *coverage*, numeric match and unverified-number counts are measured here.

---

# Stream C: market data (BE-09), verified 2026-11-01

Method: Alpaca pages fetched with WebFetch (`docs.alpaca.markets`), Finnhub from its published Swagger
(`https://finnhub.io/static/swagger.json`, which the website's JS-rendered docs page does not expose to a fetcher).
**No Alpaca or Finnhub key exists, so nothing below has run against a live service.** "Docs" = read in the pages;
"UNVERIFIED" = not stated in anything fetched, taken from SPEC 6.2 or inferred. Code is exercised only with a fake
WebSocket and respx-mocked HTTP (`tests/marketdata/test_vendor_sources.py`).

## SPEC 17.1 items owned by stream C
| # | Item | Status | Detail |
|---|---|---|---|
| 4 | Alpaca free-tier limits | **Docs: confirmed** (plan table, `docs.alpaca.markets/docs/about-market-data-api`) | Basic: real-time = IEX only; WebSocket subscriptions **30 symbols**; historical "latest 15 minutes" limitation; 200 historical API calls/min; history since 2016. The page does not say which feed the "latest 15 minutes" limit applies to, so `feed=iex` is used for REST. |
| 8 | Finnhub terms | **UNVERIFIED** | Free-plan limit "60 calls/min" and "personal/non-commercial" come from SPEC 6.2 only; the Swagger does not state them. `FinnhubRest` paces itself at 3 symbols / 5 s = 36 calls/min. |

## Alpaca WebSocket (`app/modules/marketdata/sources/alpaca.py`)
| Detail | Status |
|---|---|
| URL pattern `wss://stream.data.alpaca.markets/{version}/{feed}`; feeds `v2/sip`, `v2/iex`, `v2/delayed_sip`; test stream `wss://stream.data.alpaca.markets/v2/test` with symbol `FAKEPACA` | Docs |
| Message auth `{"action":"auth","key","secret"}` (must be sent within 10 s of connecting); header auth `APCA-API-KEY-ID`/`APCA-API-SECRET-KEY` also allowed | Docs. We use message auth. |
| Subscribe `{"action":"subscribe","trades":[...],"quotes":[...],"bars":[...]}`; channels trades, quotes, bars, updatedBars, dailyBars, statuses, lulds; `"*"` = all | Docs |
| Messages are JSON arrays; control `{"T":"success","msg":"connected"}` then `{"T":"success","msg":"authenticated"}`, `{"T":"subscription",...}`, `{"T":"error","code","msg"}` | Docs |
| Trade fields `T="t", S, i, x, p, s, c[], t (RFC-3339 ns), z`; quote fields `T="q", S, bx, bp, bs, ax, ap, as, c, z, t`; bar fields `T="b", S, o, h, l, c, v, t, n, vw` | Docs |
| Error codes 400 syntax, 401 not authenticated, 402 auth failed, 403 already authenticated, 404 auth timeout, 405 symbol limit exceeded, 406 connection limit exceeded, 407 slow client, 409 insufficient subscription, 410 invalid subscribe action, 500 internal | Docs. Every `error` message makes the stream raise `UpstreamError`; the supervisor then fails over to REST and reconnects with backoff. |
| Connection limit | Docs say "most users are limited to 1 active connection"; the Basic plan's exact number is **UNVERIFIED**. We open exactly one connection (`MarketDataService` is the only client; browsers never connect to Alpaca). |
| Whether IEX trade/quote messages are sent pre-market/after-hours, and heartbeat/ping behaviour (we rely on the `websockets` library's own ping every 20 s) | **UNVERIFIED** |
| Msgpack encoding | Docs do not mention it; we use JSON only. |

## Alpaca REST
| Detail | Status |
|---|---|
| Base `https://data.alpaca.markets`; `GET /v2/stocks/snapshots?symbols=A,B&feed=` ; `feed` values `sip`, `iex`, `delayed_sip` (15-min delayed SIP), `boats`, `overnight`, `otc`; default `sip` for the unlimited plan, `iex` otherwise | Docs |
| Snapshot fields `latestTrade{t,i,x,p,s,c,z}`, `latestQuote{t,bp,bs,bx,ap,as,ax,c,z}`, `minuteBar`, `dailyBar`, `prevDailyBar{t,o,h,l,c,v,n,vw}` | Docs. `prevDailyBar.c` is used as `prev_close`. |
| `GET /v2/stocks/bars`: `symbols`, `timeframe` (`5Min`, `1Day`...), `start`, `end`, `limit` (default 1000, max 10000), `adjustment` (`raw`/`split`/`dividend`/`spin-off`/`all`), `feed`, `page_token`, `sort`; response `{bars:{SYM:[{t,o,h,l,c,v,n,vw}]}, next_page_token}` | Docs |
| Rate-limit headers `X-RateLimit-Limit/Remaining/Reset`, HTTP 429 on excess | Docs. We map 429 to `UpstreamError` (no `Retry-After` handling; polls are 30 s apart). |
| Whether `delayed_sip` is available on the free Basic plan (SPEC 6.2 calls the free REST "15-min-delayed consolidated") | **UNVERIFIED**. We default to `feed=iex`; the quote is still labelled `alpaca_rest_delayed` and freshness `delayed`. |
| Daily-candle ingestion into `prices_daily` | `AlpacaRest.fetch_daily_bars` (pagination, `adjustment=split`) is implemented and tested against mocks; **no job calls it yet** and `prices_daily` is writable only by the owner role, so it needs `DATABASE_ADMIN_URL`. Daily candles currently come from the seed. |

## Finnhub (`sources/finnhub.py`)
| Detail | Status |
|---|---|
| Base `https://finnhub.io/api/v1`, `GET /quote?symbol=` | Swagger |
| Auth: query param `token` (security scheme `api_key`) **or** header `X-Finnhub-Token` (stated in the Swagger's Authentication section). We send the header so the key never appears in URLs/logs. | Swagger |
| Response fields `c` current price, `pc` previous close, `d` change, `dp` percent change, `o` open, `h` high, `l` low | Swagger |
| Field `t` (unix seconds) | **UNVERIFIED**: not in the `Quote` schema. Missing/zero `t` falls back to receive time. |
| `c == 0` for unknown symbols | **UNVERIFIED** (inferred); we treat a non-positive `c` as "no quote". |
| Free-plan 60 calls/min and 30 calls/s global cap | **UNVERIFIED** (SPEC only). |

## Market calendar
`pandas_market_calendars` 5.6.0 `NYSE` calendar. Checked in `tests/marketdata/test_calendar.py` against known 2026 facts:
Thanksgiving (Nov 26) closed, Black Friday (Nov 27) closes 13:00 ET = 18:00Z, Jul 3 closed (Independence Day observed),
DST switch (Mar 8) moves the UTC open from 14:30Z to 13:30Z. Pre-market 04:00 ET and post-market close+4 h are the usual
Nasdaq/NYSE extended-hours conventions, **not** read from a source here; they only affect the `pre`/`post` labels, never fills
(the paper engine trades the regular session only).

## What the simulator is (and is not)
`SimulatedSource` is a seeded mean-reverting random walk anchored to the last `prices_daily` close (design prices when absent).
It is labelled `source: "simulated"` on every quote, bar and `/market/status` and is **not market data**.
