-- 0302 (stream C / BE-15): stable machine key for auto-derived memories so consolidation can ADD / UPDATE /
-- NOOP / archive deterministically (SPEC 10.5). NULL for memories that are not auto-derived (e.g. onboarding).
alter table memories add column mem_key text;
create unique index memories_user_key_idx on memories (user_id, mem_key) where mem_key is not null;
