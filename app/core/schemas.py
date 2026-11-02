"""Shared Pydantic bits: Decimal money serialises as a JSON *number* (CONTRACT.md)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer, model_validator


def _to_number(value: Decimal) -> float:
    return float(value)


# Money: 2 dp; Price: up to 4 dp; Pct: percent numbers (6.8 == 6.8%). All Decimal internally.
Money = Annotated[Decimal, PlainSerializer(_to_number, return_type=float, when_used="json")]
Price = Money
Pct = Money


def contains_nul(value: Any) -> bool:
    """True when any string inside a (nested) JSON-like value holds a NUL character, as a key or a value."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(contains_nul(k) or contains_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_nul(v) for v in value)
    return False


class ApiModel(BaseModel):
    """Base for request/response models: strict about extras on input, snake_case keys.

    NUL (`\\u0000`) is rejected everywhere in the payload: Postgres cannot store it in text or jsonb, so it would
    otherwise surface as an HTTP 500 (SEC-07). The result is the contract 422.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_nul(cls, data: Any) -> Any:
        if contains_nul(data):
            raise ValueError("NUL characters are not allowed")
        return data


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)
