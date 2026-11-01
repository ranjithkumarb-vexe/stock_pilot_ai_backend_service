"""Paper-trading engine (FR-06): market/limit day orders, slippage, session rules, append-only ledger.

Public pieces: `engine` (pure decisions), `ledger` (pure accounting), `service` (database operations),
`context.EngineContext` (clock + price source + market state), `hooks.register_after_trade`.
There is deliberately no broker integration: every trade is simulated in our own ledger (SPEC 0.2).
"""
