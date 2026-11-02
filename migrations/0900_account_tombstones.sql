-- 0900 release hardening (SEC-13): tombstones for deleted accounts.
--
-- Problem: after DELETE /v1/account the caller's JWT stays cryptographically valid until it expires, and the API
-- provisions a `users` row for any verified subject it has not seen, so the next request resurrected an empty account.
-- Fix: remember *that* an account was deleted (and when) and refuse tokens issued at or before that moment.
--
-- Design (data minimisation / RLS):
--   * the table holds NO user_id column and no PII: only sha256(user id) and a timestamp, so the per-user export,
--     the "deletion leaves zero rows" sweeps and the RLS table list (all keyed on a `user_id` column) are unaffected;
--   * RLS is enabled and FORCED with no policy and the app role has no table privileges: the application can only go
--     through the two SECURITY DEFINER functions below, which act on the caller's own `app.user_id` and nothing else
--     (same pattern as anonymize_audit_log), so nobody can read or plant a tombstone for another account.
create table deleted_accounts (
    account_hash text primary key,
    deleted_at   timestamptz not null
);
alter table deleted_accounts enable row level security;
alter table deleted_accounts force row level security;
revoke all on table deleted_accounts from stockpilot_app;
revoke all on table deleted_accounts from public;

create function tombstone_my_account(p_deleted_at timestamptz) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
    uid text := nullif(current_setting('app.user_id', true), '');
begin
    if uid is null then
        raise exception 'tombstone_my_account: no user context' using errcode = 'insufficient_privilege';
    end if;
    insert into deleted_accounts (account_hash, deleted_at)
    values (encode(sha256(convert_to(uid, 'UTF8')), 'hex'), p_deleted_at)
    on conflict (account_hash) do update
        set deleted_at = greatest(deleted_accounts.deleted_at, excluded.deleted_at);
end
$fn$;

create function my_account_deleted_at() returns timestamptz
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $fn$
declare
    uid text := nullif(current_setting('app.user_id', true), '');
    ts  timestamptz;
begin
    if uid is null then
        return null;
    end if;
    select deleted_at into ts from deleted_accounts
     where account_hash = encode(sha256(convert_to(uid, 'UTF8')), 'hex');
    return ts;
end
$fn$;

-- DEV ONLY use: `POST /v1/auth/dev-login` (404 unless AUTH_MODE=dev) is an explicit "start over as this persona", so it lifts the
-- caller's own tombstone. Production never calls this: there a deleted subject can only come back with a token issued later.
create function clear_my_tombstone() returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
    uid text := nullif(current_setting('app.user_id', true), '');
begin
    if uid is not null then
        delete from deleted_accounts where account_hash = encode(sha256(convert_to(uid, 'UTF8')), 'hex');
    end if;
end
$fn$;

revoke all on function tombstone_my_account(timestamptz) from public;
revoke all on function my_account_deleted_at() from public;
revoke all on function clear_my_tombstone() from public;
grant execute on function tombstone_my_account(timestamptz) to stockpilot_app;
grant execute on function my_account_deleted_at() to stockpilot_app;
grant execute on function clear_my_tombstone() to stockpilot_app;
