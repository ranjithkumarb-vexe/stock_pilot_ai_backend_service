-- 0201 stream B: log the prompt file version on every LLM call (SPEC Appendix B) and the call's job outcome detail.
alter table llm_calls
    add column if not exists prompt_version text,
    add column if not exists attempts       integer not null default 1,
    add column if not exists provider       text;
