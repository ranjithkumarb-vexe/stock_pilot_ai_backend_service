"""Quote -> contract JSON (`{symbol, price, prev_close, change_pct, bid?, ask?, ts, source, session, freshness}`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.pricesource import Quote


def quote_dict(q: Quote) -> dict[str, Any]:
    change = None
    if q.prev_close is not None and q.prev_close != 0:
        change = round(float((q.price / q.prev_close - 1) * 100), 4)
    return {
        "symbol": q.symbol,
        "price": float(q.price),
        "prev_close": None if q.prev_close is None else float(q.prev_close),
        "change_pct": change,
        "bid": None if q.bid is None else float(q.bid),
        "ask": None if q.ask is None else float(q.ask),
        "ts": q.ts.isoformat().replace("+00:00", "Z"),
        "source": q.source,
        "session": q.session,
        "freshness": q.freshness,
    }


def num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
