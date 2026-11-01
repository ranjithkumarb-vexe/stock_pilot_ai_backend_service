-- 0300 (stream C / BE-09): aggregate-only view of what users watch or hold, so the market-data service can
-- compute its subscription set (SPEC 6.4 rule 1) WITHOUT admin credentials. SECURITY DEFINER bypasses RLS, so
-- the function returns counts per symbol and never a user id.
create function sp_subscription_symbols()
returns table (symbol text, watchers integer, holders integer, held_qty numeric)
language sql
stable
security definer
set search_path = public, pg_temp
as $fn$
    with w as (
        select symbol, count(*)::integer as n from watchlist_items group by symbol
    ), h as (
        select symbol, count(distinct user_id)::integer as n, sum(qty) as q
          from positions where qty > 0 group by symbol
    )
    select coalesce(w.symbol, h.symbol) as symbol,
           coalesce(w.n, 0) as watchers,
           coalesce(h.n, 0) as holders,
           coalesce(h.q, 0) as held_qty
      from w full join h on h.symbol = w.symbol
$fn$;
revoke all on function sp_subscription_symbols() from public;
grant execute on function sp_subscription_symbols() to stockpilot_app;
