from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.core.schemas import ApiModel, ResponseModel


class MeResponse(ResponseModel):
    user_id: UUID
    email: str | None
    display_name: str | None
    initials: str
    onboarded: bool
    persona_id: str | None


class DevLoginRequest(ApiModel):
    persona: Literal["priya", "alex", "sam", "new"]
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DevLoginResponse(ResponseModel):
    access_token: str
    expires_in: int
    user: MeResponse


class StreamTokenResponse(ResponseModel):
    token: str
    expires_in: int


class DeleteAccountRequest(ApiModel):
    confirm: Literal["DELETE"]
