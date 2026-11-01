"""FastAPI access to the process-wide LLM client (lazily built from settings, cached on `app.state`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.modules.llm.client import LlmClient, build_client


def get_llm(request: Request) -> LlmClient:
    client: LlmClient | None = getattr(request.app.state, "llm_client", None)
    if client is None:
        client = build_client(request.app.state.settings, request.app.state.clock)
        request.app.state.llm_client = client
    return client


LlmDep = Annotated[LlmClient, Depends(get_llm)]
