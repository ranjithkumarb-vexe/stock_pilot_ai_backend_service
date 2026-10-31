-- 0007 ops: llm_calls, audit_log (append-only), traces, stream tokens, eval runs.

create table llm_calls (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid references users (id) on delete cascade,   -- null for system jobs (owner-written)
    trace_id    text,
    job         text,
    model       text not null,
    mode        text not null check (mode in ('standard', 'contributor')),
    data_class  text not null check (data_class in ('public', 'user')),
    tokens_in   integer not null default 0,
    tokens_out  integer not null default 0,
    cost_usd    numeric(12, 6) not null default 0,
    latency_ms  integer,
    outcome     text not null default 'ok',
    created_at  timestamptz not null default now()
);
create index llm_calls_created_idx on llm_calls (created_at);
create index llm_calls_user_idx on llm_calls (user_id, created_at desc);
select sp_apply_user_rls('public.llm_calls');

-- ---------------------------------------------------------------------------------------------
-- audit_log: append-only. `user_id` deliberately has NO foreign key so deleting an account does not
-- delete (or block on) its audit trail. Account deletion calls anonymize_audit_log(uid), which nulls
-- user_id on that user's rows (the only UPDATE the trigger ever permits).
-- ---------------------------------------------------------------------------------------------
create table audit_log (
    id         bigint generated always as identity primary key,
    user_id    uuid,
    ts         timestamptz not null default now(),
    action     text not null,
    entity     text not null,
    entity_id  text,
    details    jsonb not null default '{}'::jsonb
);
create index audit_log_user_ts_idx on audit_log (user_id, ts desc);
select sp_apply_user_rls('public.audit_log');
-- the app role may only read and append
revoke update, delete, truncate on audit_log from stockpilot_app;

create function audit_log_guard() returns trigger
language plpgsql
as $fn$
begin
    if tg_op = 'UPDATE' then
        -- the single permitted mutation: anonymisation, performed by the SECURITY DEFINER function below
        -- (current_user is the function owner only inside it; the app role can never satisfy this).
        if current_setting('sp.audit_anonymize', true) = 'on'
           and current_user = (select pg_get_userbyid(proowner) from pg_proc where oid = 'audit_log_guard'::regproc)
           and old.user_id is not null and new.user_id is null
           and new.id = old.id and new.ts = old.ts and new.action = old.action
           and new.entity = old.entity and new.entity_id is not distinct from old.entity_id
        then
            return new;
        end if;
    end if;
    raise exception 'audit_log is append-only (% rejected)', tg_op using errcode = 'insufficient_privilege';
end
$fn$;

create trigger audit_log_no_update_delete before update or delete on audit_log
    for each row execute function audit_log_guard();
create trigger audit_log_no_truncate before truncate on audit_log
    for each statement execute function audit_log_guard();

-- Anonymise a deleted account's audit rows. Only callable (a) with app.user_id set to that very user and
-- (b) after the users row is gone, so nobody can scrub the attribution of a live account.
create function anonymize_audit_log(p_user_id uuid) returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
    n bigint;
begin
    if p_user_id is null
       or p_user_id is distinct from nullif(current_setting('app.user_id', true), '')::uuid then
        raise exception 'anonymize_audit_log: caller context mismatch' using errcode = 'insufficient_privilege';
    end if;
    if exists (select 1 from users where id = p_user_id) then
        raise exception 'anonymize_audit_log: account still exists' using errcode = 'insufficient_privilege';
    end if;
    perform set_config('sp.audit_anonymize', 'on', true);
    update audit_log
       set user_id = null,
           details = case when details ? 'user_hash'
                          then jsonb_build_object('anonymised', true, 'user_hash', details -> 'user_hash')
                          else jsonb_build_object('anonymised', true) end
     where user_id = p_user_id;
    get diagnostics n = row_count;
    perform set_config('sp.audit_anonymize', 'off', true);
    return n;
end
$fn$;
revoke all on function anonymize_audit_log(uuid) from public;
grant execute on function anonymize_audit_log(uuid) to stockpilot_app;

-- ---------------------------------------------------------------------------------------------
create table traces (
    id          uuid primary key default gen_random_uuid(),
    trace_id    text not null,                   -- request trace id (X-Trace-Id)
    user_id     uuid references users (id) on delete cascade,
    name        text not null,
    model       text,
    mode        text,
    duration_ms integer,
    tokens_in   integer not null default 0,
    tokens_out  integer not null default 0,
    cost_usd    numeric(12, 6) not null default 0,
    outcome     text not null default 'ok' check (outcome in ('ok', 'blocked', 'error')),
    created_at  timestamptz not null default now(),
    unique (id, user_id)
);
create index traces_user_idx on traces (user_id, created_at desc);
create index traces_trace_idx on traces (trace_id);
select sp_apply_user_rls('public.traces');

create table trace_steps (
    id         bigint generated always as identity primary key,
    trace_ref  uuid not null,
    user_id    uuid references users (id) on delete cascade,
    idx        integer not null,
    name       text not null,
    ms         integer,
    outcome    text not null default 'ok',
    detail     jsonb not null default '{}'::jsonb,
    foreign key (trace_ref, user_id) references traces (id, user_id) on delete cascade
);
create index trace_steps_trace_idx on trace_steps (trace_ref, idx);
select sp_apply_user_rls('public.trace_steps');

-- Evaluation results are global (not user data): readable by the app, written by the eval job (owner).
create table eval_runs (
    id              uuid primary key default gen_random_uuid(),
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    git_sha         text,
    thresholds_met  boolean,
    results         jsonb not null default '{}'::jsonb
);
create index eval_runs_started_idx on eval_runs (started_at desc);
select sp_apply_reference_grants('public.eval_runs');

-- ---------------------------------------------------------------------------------------------
-- Stream tokens: SSE cannot send Authorization headers, so POST /v1/stream/token mints a single-use,
-- <=60 s token. Only its sha256 is stored.
-- ---------------------------------------------------------------------------------------------
create table stream_tokens (
    token_hash  text primary key,
    user_id     uuid not null references users (id) on delete cascade,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null,
    used_at     timestamptz
);
create index stream_tokens_user_idx on stream_tokens (user_id, expires_at);
select sp_apply_user_rls('public.stream_tokens');

-- Atomic validate-and-burn without a user context (the SSE request only has the token).
create function consume_stream_token(p_hash text, p_now timestamptz) returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
    uid uuid;
begin
    update stream_tokens
       set used_at = p_now
     where token_hash = p_hash and used_at is null and expires_at > p_now
    returning user_id into uid;
    return uid;
end
$fn$;
revoke all on function consume_stream_token(text, timestamptz) from public;
grant execute on function consume_stream_token(text, timestamptz) to stockpilot_app;
