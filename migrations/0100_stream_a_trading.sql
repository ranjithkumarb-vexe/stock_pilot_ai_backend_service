-- 0100 stream A (trading domain): paper-engine settings, append-only ledger guards, split audit table,
-- proposal de-duplication, price provenance.

-- ---------------------------------------------------------------------------------------------
-- Portfolio settings (SPEC 6.5): closed-market policy for market orders and a per-order fee.
-- ---------------------------------------------------------------------------------------------
alter table portfolios
    add column after_hours_policy text not null default 'queue' check (after_hours_policy in ('queue', 'reject')),
    add column fee_per_order      numeric(18, 4) not null default 0 check (fee_per_order >= 0);

-- Where an order came from (display + audit): manual | proposal | seed
alter table orders add column source text not null default 'manual';

-- Price provenance for reference prices ('simulated' for seed/dev data, vendor name otherwise).
alter table prices_daily add column source text;
comment on column prices_daily.source is 'provenance of the row: simulated | alpaca | finnhub | ...';

-- ---------------------------------------------------------------------------------------------
-- fills and cash_ledger are append-only (SPEC 6.5). The app role loses UPDATE/DELETE/TRUNCATE and a
-- trigger rejects them for every other role too. The single exception is a DELETE fired by a foreign-key
-- cascade (pg_trigger_depth() > 1): that is account deletion / portfolio removal, never a code path.
-- ---------------------------------------------------------------------------------------------
create function ledger_append_only() returns trigger
language plpgsql
as $fn$
begin
    if tg_op = 'DELETE' and pg_trigger_depth() > 1 then
        return old;
    end if;
    raise exception '% is append-only (% rejected)', tg_table_name, tg_op using errcode = 'insufficient_privilege';
end
$fn$;

create trigger fills_append_only before update or delete on fills
    for each row execute function ledger_append_only();
create trigger fills_no_truncate before truncate on fills
    for each statement execute function ledger_append_only();
create trigger cash_ledger_append_only before update or delete on cash_ledger
    for each row execute function ledger_append_only();
create trigger cash_ledger_no_truncate before truncate on cash_ledger
    for each statement execute function ledger_append_only();
revoke update, delete, truncate on fills from stockpilot_app;
revoke update, delete, truncate on cash_ledger from stockpilot_app;

-- ---------------------------------------------------------------------------------------------
-- Corporate-action audit: one row per (portfolio, symbol, kind, ex_date) the engine applied.
-- Positions are always rebuildable from fills + corporate_actions; this table records the effect.
-- ---------------------------------------------------------------------------------------------
create table position_adjustments (
    id               bigint generated always as identity primary key,
    portfolio_id     uuid not null,
    user_id          uuid not null references users (id) on delete cascade,
    symbol           text not null references tickers (symbol),
    kind             text not null check (kind in ('split')),
    ex_date          date not null,
    ratio            numeric(18, 8) not null check (ratio > 0),
    qty_before       numeric(20, 6) not null,
    qty_after        numeric(20, 6) not null,
    avg_cost_before  numeric(18, 4) not null,
    avg_cost_after   numeric(18, 4) not null,
    applied_at       timestamptz not null default now(),
    unique (portfolio_id, symbol, kind, ex_date),
    foreign key (portfolio_id, user_id) references portfolios (id, user_id) on delete cascade
);
create index position_adjustments_user_idx on position_adjustments (user_id, applied_at);
select sp_apply_user_rls('public.position_adjustments');
revoke update, delete, truncate on position_adjustments from stockpilot_app;

-- ---------------------------------------------------------------------------------------------
-- Proposals: at most ONE pending proposal per (user, symbol, rule) - the refresh job relies on it.
-- payload->>'rule' is one of pos | sec | dd | drop | overtrading (set by the generator).
-- ---------------------------------------------------------------------------------------------
create unique index proposals_open_dedupe_idx
    on proposals (user_id, coalesce(symbol, ''), (payload ->> 'rule'))
    where status = 'pending';
