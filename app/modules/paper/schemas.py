from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, PlainSerializer, field_validator, model_validator

from app.core.schemas import ApiModel, Money, Price, ResponseModel


def _qty(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


Qty = Annotated[Decimal, PlainSerializer(_qty, return_type=float, when_used="json")]

OrderStatus = Literal["queued", "working", "filled", "cancelled", "expired", "rejected"]


class OrderRequest(ApiModel):
    symbol: str = Field(min_length=1, max_length=10)
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"]
    qty: Decimal = Field(ge=0, le=1_000_000)
    limit_price: Decimal | None = Field(default=None, gt=0, le=1_000_000, decimal_places=4)
    proposal_id: UUID | None = None
    override: bool = False

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("qty")
    @classmethod
    def _whole(cls, v: Decimal) -> Decimal:
        if v != v.to_integral_value():
            raise ValueError("fractional shares are not supported (whole shares only)")
        return v

    @model_validator(mode="after")
    def _limit(self) -> OrderRequest:
        if self.type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if self.type == "market" and self.limit_price is not None:
            raise ValueError("limit_price is only valid for limit orders")
        return self


class CheckOut(ResponseModel):
    key: str
    ok: bool
    severity: Literal["pass", "note", "blocked"]
    label: str
    detail: str


class FillOut(ResponseModel):
    id: UUID
    order_id: UUID
    symbol: str
    qty: Qty
    price: Price
    slippage_bps: Money
    fee: Money
    filled_at: datetime


class OrderOut(ResponseModel):
    id: UUID
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"]
    qty: Qty
    limit_price: Price | None
    tif: Literal["day"]
    status: OrderStatus
    source: str
    proposal_id: UUID | None
    override: bool
    reject_reason: str | None
    filled_qty: Qty
    avg_fill_price: Price | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None


class OrderDraft(ResponseModel):
    """Preview of an order that has not been placed."""

    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"]
    qty: Qty
    limit_price: Price | None
    est_price: Price | None
    est_cost: Money | None
    est_cash_after: Money | None = None
    will: Literal["fill", "queue", "work", "reject"] | None


class PlaceResponse(ResponseModel):
    order: OrderOut
    fill: FillOut | None
    checks: list[CheckOut]
    blocked: bool


class PreviewResponse(ResponseModel):
    order: OrderDraft
    fill: None = None
    checks: list[CheckOut]
    blocked: bool
    hard_blocked: bool
    market_open: bool


class OrderList(ResponseModel):
    items: list[OrderOut]
    next_cursor: str | None


def dump(model: type[ResponseModel], data: dict[str, Any]) -> dict[str, Any]:
    return model.model_validate(data).model_dump(mode="json")
