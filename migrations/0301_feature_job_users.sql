-- 0301 (stream C / BE-15): ids of the users the hourly `recompute_features` job must visit (users with recent
-- events, or with open positions). Returns ids only; features themselves are then computed inside a normal
-- per-user RLS transaction, so no admin credentials are needed at runtime.
create function sp_feature_job_user_ids(p_events_since timestamptz)
returns setof uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $fn$
    select user_id from events where received_at >= p_events_since
    union
    select user_id from positions where qty > 0
$fn$;
revoke all on function sp_feature_job_user_ids(timestamptz) from public;
grant execute on function sp_feature_job_user_ids(timestamptz) to stockpilot_app;
