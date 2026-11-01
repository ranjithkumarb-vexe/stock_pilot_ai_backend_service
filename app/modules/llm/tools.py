"""LLM tool registry (SPEC 7.4). Read-only except `create_proposal`, which only creates a *pending* proposal.

There is deliberately no tool that places an order: orders are created by the API after a user approves a
proposal. `tests/safety/test_toolscan.py` scans this registry (T-SEC-07 / NFR-04). Modules that add tools call
`register_tool()`; the scan test imports the registry after all modules are loaded.
"""

from __future__ import annotations

from typing import Any

TOOL_REGISTRY: dict[str, dict[str, Any]] = {}


def register_tool(name: str, description: str, parameters: dict[str, Any]) -> None:
    TOOL_REGISTRY[name] = {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S = {"type": "string"}
register_tool(
    "get_xbrl_metric",
    "Read one XBRL metric for a ticker and period.",
    _obj({"ticker": _S, "concept": _S, "period": _S}, ["ticker", "concept"]),
)
register_tool(
    "search_filings",
    "Hybrid search over a ticker's filing chunks.",
    _obj(
        {"ticker": _S, "query": _S, "sections": {"type": "array", "items": _S}, "k": {"type": "integer"}},
        ["ticker", "query"],
    ),
)
register_tool(
    "compute_ratio",
    "Compute a named ratio from supplied inputs (code, not the model).",
    _obj({"name": _S, "inputs": {"type": "object"}}, ["name", "inputs"]),
)
register_tool(
    "get_quotes", "Read latest quotes.", _obj({"tickers": {"type": "array", "items": _S}}, ["tickers"])
)
register_tool(
    "get_portfolio", "Read the signed-in user's paper portfolio.", _obj({"user_id": _S}, ["user_id"])
)
register_tool(
    "evaluate_risk",
    "Run the deterministic risk engine on a portfolio against rules.",
    _obj({"portfolio": {"type": "object"}, "rules": {"type": "object"}}, ["portfolio", "rules"]),
)
register_tool(
    "create_proposal",
    "Create a PENDING proposal for the user to approve or reject. Never executes a trade.",
    _obj({"type": _S, "ticker": _S, "rationale_ids": {"type": "array", "items": _S}}, ["type", "ticker"]),
)
register_tool(
    "get_user_profile",
    "Read selected fields of the user's profile.",
    _obj({"user_id": _S, "fields": {"type": "array", "items": _S}}, ["user_id"]),
)
