"""Tool-registry scan (NFR-04, T-SEC-07): no LLM-callable tool may place, submit or execute an order.

Names are split into word tokens (snake_case / camelCase). A tool fails if any token is an order-placing verb,
or if it mentions `order(s)` without a read-only verb first. `create_proposal` (pending proposal only) passes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

FORBIDDEN_VERBS = frozenset(
    {"place", "submit", "execute", "buy", "sell", "trade", "transfer", "withdraw", "wire", "liquidate",
     "short", "fill", "route", "broker", "send", "cancel", "modify", "approve", "purchase", "invest"}
)  # fmt: skip
READ_VERBS = frozenset(
    {"get", "list", "read", "search", "describe", "explain", "compute", "evaluate", "fetch"}
)
_TOKEN = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def tokens(name: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(name.replace("-", "_").replace("_", " ").replace(".", " "))]


def tool_violations(name: str) -> list[str]:
    toks = tokens(name)
    out = [f"forbidden verb {t!r}" for t in toks if t in FORBIDDEN_VERBS]
    if any(t in ("order", "orders") for t in toks) and (not toks or toks[0] not in READ_VERBS):
        out.append("mentions orders without a read-only verb")
    return out


def scan_tool_names(names: Iterable[str]) -> dict[str, list[str]]:
    return {n: v for n in names if (v := tool_violations(n))}
