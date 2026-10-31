-- 0006 personalization (SPEC §8 "personalization"). All user-owned. Embeddings cascade-delete with the user.

create table events (
    event_id        uuid not null,
    user_id         uuid not null references users (id) on delete cascade,
    ts              timestamptz not null,
    session_id      text,
    type            text not null,
    entity          jsonb not null default '{}'::jsonb,
    props           jsonb not null default '{}'::jsonb,
    context         jsonb not null default '{}'::jsonb,
    client          jsonb not null default '{}'::jsonb,
    schema_version  integer not null default 1,
    received_at     timestamptz not null default now(),
    -- deviation from SPEC (`event_id pk`): scoped per user so one tenant cannot probe/claim another's ids
    primary key (user_id, event_id)
);
create index events_user_ts_idx on events (user_id, ts desc);
create index events_user_type_idx on events (user_id, type, ts desc);
select sp_apply_user_rls('public.events');

create table user_features (
    user_id         uuid not null references users (id) on delete cascade,
    key             text not null,
    value           jsonb not null,
    confidence      numeric(5, 4) not null default 0 check (confidence between 0 and 1),
    half_life_days  integer,
    updated_at      timestamptz not null default now(),
    primary key (user_id, key)
);
select sp_apply_user_rls('public.user_features');

create table user_interest_vector (
    user_id     uuid not null references users (id) on delete cascade,
    dim         text not null,                   -- sector:tech | theme:ai | factor:value | cap:large
    weight      numeric(8, 6) not null default 0,
    baseline    numeric(8, 6) not null default 0,
    updated_at  timestamptz not null default now(),
    primary key (user_id, dim)
);
select sp_apply_user_rls('public.user_interest_vector');

create table bandit_arms (
    user_id     uuid not null references users (id) on delete cascade,
    arm         text not null,
    context     text not null default '',
    alpha       numeric(12, 4) not null default 1 check (alpha > 0),
    beta        numeric(12, 4) not null default 1 check (beta > 0),
    updated_at  timestamptz not null default now(),
    primary key (user_id, arm, context)
);
select sp_apply_user_rls('public.bandit_arms');

create table memories (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid not null references users (id) on delete cascade,
    kind             text not null check (kind in ('semantic', 'episodic', 'procedural')),
    text             text not null,
    embedding        vector(1024),
    source           text,
    source_event_ids uuid[] not null default '{}',
    confidence       numeric(5, 4) not null default 0.5 check (confidence between 0 and 1),
    created_at       timestamptz not null default now(),
    last_used_at     timestamptz,
    archived         boolean not null default false
);
create index memories_user_idx on memories (user_id, archived, created_at desc);
create index memories_embedding_idx on memories using hnsw (embedding vector_cosine_ops);
select sp_apply_user_rls('public.memories');

create table personalization_audit (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references users (id) on delete cascade,
    output_type    text not null,
    output_id      text not null,
    basis          text check (basis in ('profile', 'behaviour', 'cold_start')),
    features_used  jsonb not null default '{}'::jsonb,
    arm_scores     jsonb not null default '{}'::jsonb,
    rules_checked  jsonb not null default '[]'::jsonb,
    created_at     timestamptz not null default now()
);
create index personalization_audit_output_idx on personalization_audit (user_id, output_type, output_id);
select sp_apply_user_rls('public.personalization_audit');
