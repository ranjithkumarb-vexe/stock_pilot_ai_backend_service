"""Ranker (BE-16, SPEC 10.6): candidates -> risk/exclusion filter -> score -> MMR -> pinned criticals.

Kept import-light on purpose (alerts <-> ranking depend on each other's leaf modules):
`scoring`/`metrics` are pure, `bandit` talks to `bandit_arms`, `candidates`/`briefing`/`why`/`service` build the feed.
"""
