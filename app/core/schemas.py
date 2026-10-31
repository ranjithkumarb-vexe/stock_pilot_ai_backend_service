"""Shared Pydantic bits: Decimal money serialises as a JSON *number* (CONTRACT.md)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _to_number(value: Decimal) -> float:
    return float(value)


# Money: 2 dp; Price: up to 4 dp; Pct: percent numbers (6.8 == 6.8%). All Decimal internally.
Money = Annotated[Decimal, PlainSerializer(_to_number, return_type=float, when_used="json")]
Price = Money
Pct = Money


class ApiModel(BaseModel):
    """Base for request/response models: strict about extras on input, snake_case keys."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)
