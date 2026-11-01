"""Small helpers shared by the trading API tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx

from app.core.pricesource import Quote


async def login(client: httpx.AsyncClient, persona: str) -> tuple[dict[str, str], UUID]:
    r = await client.post("/v1/auth/dev-login", json={"persona": persona})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, UUID(r.json()["user"]["user_id"])


async def place(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    symbol: str,
    side: str,
    qty: int | float,
    *,
    type: str = "market",
    limit_price: float | None = None,
    key: str | None = None,
    **extra: Any,
) -> httpx.Response:
    body: dict[str, Any] = {"symbol": symbol, "side": side, "type": type, "qty": qty, **extra}
    if limit_price is not None:
        body["limit_price"] = limit_price
    return await client.post(
        "/v1/orders", headers={**headers, "Idempotency-Key": key or str(uuid4())}, json=body
    )


def dec(x: Any) -> Decimal:
    return Decimal(str(x))


class StaleSource:
    """Wraps a PriceSource and marks every quote stale (T-DATA-02)."""

    name = "stale-test"

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def get_last(self, symbol: str) -> Quote:
        q: Quote = await self._inner.get_last(symbol)
        return Quote(q.symbol, q.price, q.ts, q.source, q.prev_close, q.bid, q.ask, q.session, "stale")

    def subscribe(self, symbols: Any) -> Any:
        return self._inner.subscribe(symbols)
