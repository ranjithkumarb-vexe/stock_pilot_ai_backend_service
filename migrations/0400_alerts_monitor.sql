-- 0400 (stream D / BE-17): ids the alert monitor must visit, and the symbols it must poll, without admin credentials
-- at runtime. Both return identifiers only; alerts themselves are evaluated inside a normal per-user RLS transaction.
create function sp_alert_job_user_ids()
returns setof uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $fn$
    select user_id from positions where qty > 0
    union
    select user_id from watchlist_items
$fn$;
revoke all on function sp_alert_job_user_ids() from public;
grant execute on function sp_alert_job_user_ids() to stockpilot_app;

create function sp_alert_job_symbols()
returns setof text
language sql
stable
security definer
set search_path = public, pg_temp
as $fn$
    select symbol from positions where qty > 0
    union
    select symbol from watchlist_items
$fn$;
revoke all on function sp_alert_job_symbols() from public;
grant execute on function sp_alert_job_symbols() to stockpilot_app;

-- alert queries by (user, breach key / suppression group) for the 24 h suppression check
create index if not exists alerts_user_suppress_idx on alerts (user_id, ((payload ->> 'suppress_key')), created_at desc);
