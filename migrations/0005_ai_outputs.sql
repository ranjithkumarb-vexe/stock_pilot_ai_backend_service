-- 0005 AI outputs: briefs (+ citations), alerts, chat.

create table briefs (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references users (id) on delete cascade,
    symbol        text not null references tickers (symbol),
    as_of         date not null,
    style_hash    text not null,                 -- cache key = (user_id, symbol, as_of, style_hash)
    style_card    jsonb not null default '{}'::jsonb,
    ledger        jsonb not null default '{}'::jsonb,
    content       jsonb not null default '{}'::jsonb,
    rendered_md   text,
    status        text not null default 'ok' check (status in ('ok', 'degraded')),
    stale         boolean not null default false,
    stale_reason  text,
    trace_id      text,
    created_at    timestamptz not null default now(),
    unique (user_id, symbol, as_of, style_hash),
    unique (id, user_id)
);
create index briefs_user_symbol_idx on briefs (user_id, symbol, created_at desc);
select sp_apply_user_rls('public.briefs');

create table citations (
    id          uuid primary key default gen_random_uuid(),
    brief_id    uuid not null,
    user_id     uuid not null references users (id) on delete cascade,
    kind        text not null check (kind in ('chunk', 'xbrl', 'price')),
    ref_id      text not null,
    locator     text,
    quote_hash  text,
    created_at  timestamptz not null default now(),
    foreign key (brief_id, user_id) references briefs (id, user_id) on delete cascade
);
create index citations_brief_idx on citations (brief_id);
select sp_apply_user_rls('public.citations');

create table alerts (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references users (id) on delete cascade,
    symbol         text references tickers (symbol),
    type           text not null check (type in ('breach', 'filing', 'move', 'earnings', 'mirror', 'concentration')),
    materiality    numeric(5, 4) not null default 0 check (materiality between 0 and 1),
    critical       boolean not null default false,
    payload        jsonb not null default '{}'::jsonb,   -- title, body, source_label, why ...
    dedupe_key     text not null,
    state          text not null default 'new' check (state in ('new', 'opened', 'dismissed', 'snoozed')),
    snoozed_until  timestamptz,
    created_at     timestamptz not null default now(),
    delivered_at   timestamptz,
    unique (user_id, dedupe_key)
);
create index alerts_user_created_idx on alerts (user_id, created_at desc);
create index alerts_user_state_idx on alerts (user_id, state);
select sp_apply_user_rls('public.alerts');

create table chat_threads (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references users (id) on delete cascade,
    title       text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (id, user_id)
);
create index chat_threads_user_idx on chat_threads (user_id, updated_at desc);
select sp_apply_user_rls('public.chat_threads');

create table chat_messages (
    id              uuid primary key default gen_random_uuid(),
    thread_id       uuid not null,
    user_id         uuid not null references users (id) on delete cascade,
    role            text not null check (role in ('user', 'assistant')),
    content         text not null,
    citations       jsonb not null default '[]'::jsonb,
    refused         boolean not null default false,
    refusal_reason  text,
    trace_id        text,
    created_at      timestamptz not null default now(),
    foreign key (thread_id, user_id) references chat_threads (id, user_id) on delete cascade
);
create index chat_messages_thread_idx on chat_messages (thread_id, created_at);
select sp_apply_user_rls('public.chat_messages');
