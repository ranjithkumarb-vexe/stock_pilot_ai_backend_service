-- 0002 reference tables (SPEC §8 "reference"): shared, not user-owned. App role: SELECT only.

create table tickers (
    symbol       text primary key check (symbol ~ '^[A-Z0-9.\-]{1,10}$'),
    cik          text,
    name         text not null,
    sector       text,
    industry     text,
    exchange     text,
    in_universe  boolean not null default true,
    created_at   timestamptz not null default now()
);
create index tickers_universe_idx on tickers (sector) where in_universe;

create table xbrl_facts (
    id            bigint generated always as identity primary key,
    symbol        text not null references tickers (symbol) on delete cascade,
    concept       text not null,                 -- e.g. us-gaap:Revenues
    unit          text not null,
    period_start  date,                          -- null for instant (balance-sheet) facts
    period_end    date not null,
    fy            integer,
    fp            text,
    value         numeric not null,
    accession_no  text not null,
    form          text,
    filed_at      timestamptz not null,
    constraint xbrl_facts_natural_key
        unique nulls not distinct (symbol, concept, unit, period_start, period_end, accession_no)
);
create index xbrl_facts_lookup_idx on xbrl_facts (symbol, concept, period_end desc);
create index xbrl_facts_filed_idx on xbrl_facts (symbol, filed_at);

create table raw_documents (
    id                 uuid primary key default gen_random_uuid(),
    symbol             text not null references tickers (symbol),
    form               text not null,
    accession_no       text not null,
    title              text,
    url                text not null,
    filed_at           timestamptz not null,
    period_of_report   date,
    fetched_at         timestamptz not null default now(),
    sha256             text not null,
    storage_path       text,
    quarantined        boolean not null default false,
    quarantine_reason  text,
    unique (symbol, accession_no)
);
create index raw_documents_symbol_idx on raw_documents (symbol, filed_at desc);

-- dim 1024 set by the chosen embedding model (ADR-005: deterministic local hashing embedding for now)
create table doc_chunks (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null references raw_documents (id) on delete cascade,
    symbol        text not null references tickers (symbol),
    form          text,
    accession_no  text,
    filed_at      timestamptz,                   -- denormalised for the `as_of` guard
    item          text,
    period        text,
    anchor        text,
    chunk_no      integer not null default 0,
    text          text not null,
    token_count   integer,
    quarantined   boolean not null default false,
    embedding     vector(1024),
    tsv           tsvector generated always as (to_tsvector('english', coalesce(text, ''))) stored
);
create index doc_chunks_tsv_idx on doc_chunks using gin (tsv);
create index doc_chunks_embedding_idx on doc_chunks using hnsw (embedding vector_cosine_ops);
create index doc_chunks_symbol_idx on doc_chunks (symbol, item);
create index doc_chunks_document_idx on doc_chunks (document_id, chunk_no);

create table prices_daily (
    symbol     text not null references tickers (symbol) on delete cascade,
    date       date not null,
    open       numeric(18, 4),
    high       numeric(18, 4),
    low        numeric(18, 4),
    close      numeric(18, 4) not null,
    adj_close  numeric(18, 4),
    volume     bigint,
    primary key (symbol, date)
);

create table corporate_actions (
    id        bigint generated always as identity primary key,
    symbol    text not null references tickers (symbol) on delete cascade,
    type      text not null check (type in ('split', 'dividend')),
    ex_date   date not null,
    ratio     numeric(18, 8),                    -- split ratio, e.g. 4 for a 4-for-1 split
    amount    numeric(18, 6),                    -- cash dividend per share
    unique (symbol, type, ex_date)
);

select sp_apply_reference_grants('public.tickers');
select sp_apply_reference_grants('public.xbrl_facts');
select sp_apply_reference_grants('public.raw_documents');
select sp_apply_reference_grants('public.doc_chunks');
select sp_apply_reference_grants('public.prices_daily');
select sp_apply_reference_grants('public.corporate_actions');
