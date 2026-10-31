-- 0001 foundation: extensions, application role, grants, RLS helper.
-- Must be applied by the OWNER role (superuser / Supabase `postgres`): SECURITY DEFINER functions defined
-- later rely on the owner bypassing row-level security.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = current_user and (rolsuper or rolbypassrls)) then
        raise exception 'migrations must run as a superuser or BYPASSRLS role (current_user=%)', current_user;
    end if;
end $$;

create extension if not exists pgcrypto;
create extension if not exists "uuid-ossp";
create extension if not exists pg_trgm;
create extension if not exists vector;

-- Application role used by the API pool. NOT a superuser, NOT BYPASSRLS => RLS is enforced.
-- The password below is a DEV DEFAULT only. Deployments MUST override it: the migrate CLI runs
-- `ALTER ROLE stockpilot_app PASSWORD <APP_DB_PASSWORD>` after migrating (see app/core/migrate.py).
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'stockpilot_app') then
        create role stockpilot_app login password 'stockpilot_dev_pw';
    end if;
end $$;
alter role stockpilot_app login nosuperuser nobypassrls nocreatedb nocreaterole noinherit;

do $$
begin
    execute format('grant connect on database %I to stockpilot_app', current_database());
end $$;

grant usage on schema public to stockpilot_app;
-- Tables created by later migrations are readable/writable by the app role by default; reference tables
-- and append-only tables explicitly REVOKE what they must not allow (see 0002, 0007, 0008).
alter default privileges in schema public grant select, insert, update, delete on tables to stockpilot_app;
alter default privileges in schema public grant usage, select on sequences to stockpilot_app;

-- Row-level security helper: every user-owned table calls this in the SAME migration that creates it.
--   select sp_apply_user_rls('public.my_table');            -- owner column `user_id`
--   select sp_apply_user_rls('public.users', 'id');          -- other owner column
-- Effect: ENABLE + FORCE RLS and policy `tenant_isolation`:
--   user_id = nullif(current_setting('app.user_id', true), '')::uuid   (USING and WITH CHECK)
-- With no `app.user_id` the expression is NULL => no rows visible, no rows writable.
create or replace function sp_apply_user_rls(tbl regclass, col text default 'user_id')
returns void
language plpgsql
as $fn$
begin
    execute format('alter table %s enable row level security', tbl);
    execute format('alter table %s force row level security', tbl);
    execute format('drop policy if exists tenant_isolation on %s', tbl);
    execute format(
        $p$create policy tenant_isolation on %s for all
           using (%I = nullif(current_setting('app.user_id', true), '')::uuid)
           with check (%I = nullif(current_setting('app.user_id', true), '')::uuid)$p$,
        tbl, col, col);
end
$fn$;

-- Reference-table helper: readable by the app role, writable only by the owner.
--   select sp_apply_reference_grants('public.tickers');
create or replace function sp_apply_reference_grants(tbl regclass)
returns void
language plpgsql
as $fn$
begin
    execute format('revoke insert, update, delete, truncate on %s from stockpilot_app', tbl);
    execute format('grant select on %s to stockpilot_app', tbl);
end
$fn$;

revoke all on function sp_apply_user_rls(regclass, text) from public;
revoke all on function sp_apply_reference_grants(regclass) from public;
