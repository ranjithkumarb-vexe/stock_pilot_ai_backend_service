-- 0200 stream B: filing-ingest bookkeeping, structured tables extracted from filings, global LLM spend.

-- resumable ingest: a document is "done" when chunk_count is not null and ingest_version matches
alter table raw_documents
    add column if not exists chunk_count     integer,
    add column if not exists ingest_version  text,
    add column if not exists parsed_at       timestamptz,
    add column if not exists sections_found  text[];

-- HTML tables are kept as structured JSON, not prose (SPEC 7.5 step 4). Shared reference data.
create table doc_tables (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null references raw_documents (id) on delete cascade,
    symbol        text not null references tickers (symbol),
    form          text,
    accession_no  text,
    filed_at      timestamptz,
    item          text,
    idx           integer not null,
    n_rows        integer not null,
    n_cols        integer not null,
    preview       text,
    rows          jsonb not null,
    unique (document_id, idx)
);
create index doc_tables_symbol_idx on doc_tables (symbol, item);
select sp_apply_reference_grants('public.doc_tables');

-- Global LLM spend across all users (budget alert / hard stop). RLS hides other tenants' llm_calls from the
-- app role, so the aggregate is exposed through a SECURITY DEFINER function that returns only the sum.
create or replace function llm_spend_global(p_since timestamptz) returns numeric
language sql
security definer
stable
set search_path = public, pg_temp
as $fn$
    select coalesce(sum(cost_usd), 0) from llm_calls where created_at >= p_since;
$fn$;
revoke all on function llm_spend_global(timestamptz) from public;
grant execute on function llm_spend_global(timestamptz) to stockpilot_app;
