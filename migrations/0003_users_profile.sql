-- 0003 users + profile tables (all user-owned => RLS in this migration).

-- `users.id` = auth subject (Supabase `sub` / dev-login id). Created on first authenticated request.
create table users (
    id            uuid primary key,
    email         text,
    display_name  text,
    persona_id    text,                          -- priya | alex | sam for demo personas, else null
    onboarded_at  timestamptz,
    created_at    timestamptz not null default now()
);
create index users_email_idx on users (lower(email));
select sp_apply_user_rls('public.users', 'id');

create table profiles (
    user_id            uuid primary key references users (id) on delete cascade,
    risk_tier_stated   integer not null check (risk_tier_stated between 1 and 5),
    horizon            text,
    goals              text[] not null default '{}',
    experience         text,
    region             text,
    currency           text not null default 'USD',
    esg_exclusions     text[] not null default '{}',
    disclaimer_ack_at  timestamptz,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);
select sp_apply_user_rls('public.profiles');

create table risk_rules (
    user_id     uuid not null references users (id) on delete cascade,
    rule_key    text not null,                   -- pos | sec | dd | trades | cash
    value       numeric not null,
    source      text not null default 'default' check (source in ('default', 'user')),
    updated_at  timestamptz not null default now(),
    primary key (user_id, rule_key)
);
select sp_apply_user_rls('public.risk_rules');

create table watchlist_items (
    user_id   uuid not null references users (id) on delete cascade,
    symbol    text not null references tickers (symbol),
    added_at  timestamptz not null default now(),
    primary key (user_id, symbol)
);
select sp_apply_user_rls('public.watchlist_items');

create table sector_prefs (
    user_id  uuid not null references users (id) on delete cascade,
    sector   text not null,
    pref     smallint not null check (pref in (-1, 1)),   -- +1 like, -1 exclude
    primary key (user_id, sector)
);
select sp_apply_user_rls('public.sector_prefs');
