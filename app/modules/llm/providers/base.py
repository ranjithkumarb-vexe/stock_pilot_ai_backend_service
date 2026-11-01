from __future__ import annotations

from typing import Protocol

from app.modules.llm.types import ProviderRequest, ProviderResult


class Provider(Protocol):
    name: str

    async def complete(self, req: ProviderRequest) -> ProviderResult:
        """One attempt. Raise `ProviderError(retriable=...)` on failure; never retry internally."""
        ...

    async def aclose(self) -> None: ...
