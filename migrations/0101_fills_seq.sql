-- 0101 (stream A): total order of fills. `filled_at` alone can tie (several fills in one sweep / one second), and
-- positions are rebuilt by replaying fills, so replay order must equal execution order. `seq` is assigned
-- under the portfolio row lock, i.e. in execution order.
alter table fills add column seq bigint generated always as identity;
create index fills_portfolio_seq_idx on fills (portfolio_id, seq);
