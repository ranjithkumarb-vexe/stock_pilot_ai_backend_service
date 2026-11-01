-- 0500 stream E: chat answer bookkeeping + read access to *system* traces (jobs without a user).

-- What the answer was built from, for the audit trail: routed intent and the numeric ledger the answer's
-- numbers came from (every figure in an assistant message appears here; user messages leave both empty).
alter table chat_messages
    add column if not exists intent text,
    add column if not exists ledger jsonb not null default '{}'::jsonb;
create index if not exists chat_threads_user_updated_idx on chat_threads (user_id, updated_at desc);

-- System traces (e.g. a filing chunk quarantined by the injection scanner) have `user_id is null`, which the
-- tenant policy hides from every user. They contain no user data and the Audit screen shows them, so they are
-- exposed through two SECURITY DEFINER read functions rather than by weakening the tenant policy.
create or replace function audit_system_traces(p_limit integer default 50)
returns setof traces
language sql
security definer
stable
set search_path = public, pg_temp
as $fn$
    select * from traces where user_id is null order by created_at desc limit least(greatest(p_limit, 1), 200);
$fn$;

create or replace function audit_system_trace_steps(p_trace uuid)
returns setof trace_steps
language sql
security definer
stable
set search_path = public, pg_temp
as $fn$
    select s.* from trace_steps s join traces t on t.id = s.trace_ref where t.id = p_trace and t.user_id is null
    order by s.idx;
$fn$;

revoke all on function audit_system_traces(integer) from public;
revoke all on function audit_system_trace_steps(uuid) from public;
grant execute on function audit_system_traces(integer) to stockpilot_app;
grant execute on function audit_system_trace_steps(uuid) to stockpilot_app;
