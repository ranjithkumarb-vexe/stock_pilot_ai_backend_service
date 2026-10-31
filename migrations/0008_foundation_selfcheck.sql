-- 0008 foundation self-check. Fails the migration if a table created so far is neither tenant-isolated
-- (RLS enabled + forced + a policy) nor a read-only reference table. Later migrations are covered by
-- tests/db/test_rls.py, which scans the live catalog.

-- the runner's bookkeeping table is not for the app role
revoke all on schema_migrations from stockpilot_app;

do $$
declare
    r record;
    problems text := '';
    reference_tables text[] := array['tickers', 'xbrl_facts', 'raw_documents', 'doc_chunks',
                                     'prices_daily', 'corporate_actions', 'eval_runs', 'schema_migrations'];
begin
    for r in
        select c.relname, c.relrowsecurity, c.relforcerowsecurity,
               exists (select 1 from pg_policy p where p.polrelid = c.oid) as has_policy
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r'
    loop
        if r.relname = any (reference_tables) then
            if has_table_privilege('stockpilot_app', format('public.%I', r.relname), 'INSERT')
               or has_table_privilege('stockpilot_app', format('public.%I', r.relname), 'UPDATE')
               or has_table_privilege('stockpilot_app', format('public.%I', r.relname), 'DELETE') then
                if r.relname <> 'schema_migrations' then
                    problems := problems || format(' reference table %s is writable by app role;', r.relname);
                end if;
            end if;
        elsif not (r.relrowsecurity and r.relforcerowsecurity and r.has_policy) then
            problems := problems || format(' table %s lacks forced RLS + policy;', r.relname);
        end if;
    end loop;
    if problems <> '' then
        raise exception 'foundation self-check failed:%', problems;
    end if;
end $$;
