"""One live call through the single client: `uv run python -m app.modules.llm.smoke`.

Exit 0 = real model answered; exit 2 = BLOCKED (no key / provider is mock); exit 1 = the call degraded.
Records nothing in the database (in-memory usage store)."""

from __future__ import annotations

import asyncio
import json
import sys

from app.core.config import get_settings
from app.modules.llm.budget import InMemoryUsageStore
from app.modules.llm.client import build_client
from app.modules.llm.types import LlmRequest, Message


async def main() -> int:
    settings = get_settings()
    if settings.llm_provider != "meta" or not settings.meta_model_api_key:
        print(
            json.dumps({"status": "blocked", "reason": "LLM_PROVIDER!=meta or META_MODEL_API_KEY is empty"})
        )
        return 2
    client = build_client(settings, store=InMemoryUsageStore())
    try:
        resp = await client.call(
            LlmRequest(
                job="chat_qa",
                messages=[Message("user", "Reply with the single word: ok")],
                data_class="public",
                max_tokens=16,
            )
        )
    finally:
        await client.aclose()
    print(json.dumps({"status": "ok" if resp.ok else "degraded", "model": resp.model, "reason": resp.degraded_reason,
                      "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out, "attempts": resp.attempts}))  # fmt: skip
    return 0 if resp.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
