"""Alert row -> CONTRACT alert JSON (shared by `/alerts`, the feed and SSE)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.isoformat().replace("+00:00", "Z")


def alert_view(row: Any, why: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = row["payload"] or {}
    view: dict[str, Any] = {
        "id": str(row["id"]),
        "type": row["type"],
        "symbol": row["symbol"],
        "critical": bool(row["critical"]),
        "materiality": float(row["materiality"]),
        "title": payload.get("title", ""),
        "body": payload.get("body", ""),
        "source_label": payload.get("source_label", ""),
        "state": row["state"],
        "created_at": iso(row["created_at"]),
        "snoozed_until": iso(row["snoozed_until"]),
    }
    if why is not None:
        view["why"] = why
    return view
