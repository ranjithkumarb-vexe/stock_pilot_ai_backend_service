-- 0004 paper trading. user_id is denormalised onto every row (fast RLS, no joins in policies) and tied to
-- the owning portfolio/order/proposal by composite foreign keys, so a row can never point at another
-- user's parent. Positions are derived data and rebuildable from fills.

create table portfolios (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references users (id) on delete cascade,
    name           text not null default 'Main',
    starting_cash  numeric(18, 2) not null check (starting_cash >= 0),
    created_at     timestamptz not null default now(),
    unique (user_id, name),
    unique (id, user_id)
);
select sp_apply_user_rls('public.portfolios');

create table proposals (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references users (id) on delete cascade,
    portfolio_id  uuid not null,
    type          text not null check (type in ('trade', 'review')),
    symbol        text references tickers (symbol),
    payload       jsonb not null default '{}'::jsonb,
    rationale     jsonb not null default '[]'::jsonb,
    rules_checked jsonb not null default '[]'::jsonb,
    status        text not null default 'pending'
                  check (status in ('pending', 'approved', 'rejected', 'modified', 'expired')),
    reject_reason text check (reject_reason in ('too_risky', 'disagree', 'timing', 'already_know')),
    modified_qty  numeric(20, 6),
    created_at    timestamptz not null default now(),
    expires_at    timestamptz,
    decided_at    timestamptz,
    unique (id, user_id),
    foreign key (portfolio_id, user_id) references portfolios (id, user_id) on delete cascade
);
create index proposals_user_status_idx on proposals (user_id, status, created_at desc);
select sp_apply_user_rls('public.proposals');

create table orders (
    id               uuid primary key default gen_random_uuid(),
    portfolio_id     uuid not null,
    user_id          uuid not null references users (id) on delete cascade,
    symbol           text not null references tickers (symbol),
    side             text not null check (side in ('buy', 'sell')),
    type             text not null check (type in ('market', 'limit')),
    qty              numeric(20, 6) not null check (qty > 0),
    limit_price      numeric(18, 4) check (limit_price is null or limit_price > 0),
    tif              text not null default 'day' check (tif in ('day')),
    status           text not null default 'queued'
                     check (status in ('queued', 'working', 'filled', 'cancelled', 'expired', 'rejected')),
    proposal_id      uuid,
    idempotency_key  text not null,
    override         boolean not null default false,
    reject_reason    text,
    checks           jsonb not null default '[]'::jsonb,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    expires_at       timestamptz,
    check (type = 'market' or limit_price is not null),
    unique (user_id, idempotency_key),
    unique (id, user_id),
    foreign key (portfolio_id, user_id) references portfolios (id, user_id) on delete cascade,
    foreign key (proposal_id, user_id) references proposals (id, user_id) on delete set null (proposal_id)
);
create index orders_user_status_idx on orders (user_id, status);
create index orders_portfolio_idx on orders (portfolio_id, created_at desc);
select sp_apply_user_rls('public.orders');

create table fills (
    id            uuid primary key default gen_random_uuid(),
    order_id      uuid not null,
    user_id       uuid not null references users (id) on delete cascade,
    portfolio_id  uuid not null,
    symbol        text not null references tickers (symbol),
    qty           numeric(20, 6) not null check (qty > 0),
    price         numeric(18, 4) not null check (price > 0),
    slippage_bps  numeric(10, 4) not null default 0,
    fee           numeric(18, 4) not null default 0,
    filled_at     timestamptz not null default now(),
    foreign key (order_id, user_id) references orders (id, user_id) on delete cascade,
    foreign key (portfolio_id, user_id) references portfolios (id, user_id) on delete cascade
);
create index fills_portfolio_idx on fills (portfolio_id, filled_at);
create index fills_order_idx on fills (order_id);
select sp_apply_user_rls('public.fills');

create table cash_ledger (
    id            bigint generated always as identity primary key,
    portfolio_id  uuid not null,
    user_id       uuid not null references users (id) on delete cascade,
    ts            timestamptz not null default now(),
    kind          text not null check (kind in ('deposit', 'fill', 'fee', 'dividend', 'adjustment')),
    amount        numeric(18, 4) not null,          -- signed: + into cash, - out of cash
    ref_id        uuid,
    foreign key (portfolio_id, user_id) references portfolios (id, user_id) on delete cascade
);
create index cash_ledger_portfolio_idx on cash_ledger (portfolio_id, ts);
select sp_apply_user_rls('public.cash_ledger');

create table positions (
    portfolio_id  uuid not null,
    symbol        text not null references tickers (symbol),
    user_id       uuid not null references users (id) on delete cascade,
    qty           numeric(20, 6) not null check (qty >= 0),
    avg_cost      numeric(18, 4) not null check (avg_cost >= 0),
    updated_at    timestamptz not null default now(),
    primary key (portfolio_id, symbol),
    foreign key (portfolio_id, user_id) references portfolios (id, user_id) on delete cascade
);
create index positions_user_idx on positions (user_id);
select sp_apply_user_rls('public.positions');

create table equity_snapshots (
    portfolio_id     uuid not null,
    user_id          uuid not null references users (id) on delete cascade,
    ts               timestamptz not null,
    equity           numeric(18, 2) not null,
    cash             numeric(18, 2) not null,
    spy_equity_norm  numeric(18, 6),
    primary key (portfolio_id, ts),
    foreign key (portfolio_id, user_id) references portfolios (id, user_id) on delete cascade
);
create index equity_snapshots_user_idx on equity_snapshots (user_id, ts);
select sp_apply_user_rls('public.equity_snapshots');
