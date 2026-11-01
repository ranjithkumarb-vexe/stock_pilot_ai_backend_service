"""Meta Model API via its OpenAI-compatible Chat Completions endpoint (SPEC 6.3).

Everything below the request/response *shape* is UNVERIFIED against the real service (no API key available;
see docs/VERIFIED.md): the exact name of the reasoning parameter, `response_format` json_schema support, usage
field names and Retry-After behaviour follow the OpenAI convention the SPEC says the API follows. The unit
tests exercise this class against a mocked transport only.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.modules.llm.types import VALID_EFFORTS, ProviderError, ProviderRequest, ProviderResult

RETRIABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class MetaProvider:
    name = "meta"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("META_MODEL_API_KEY is required for MetaProvider")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def build_payload(req: ProviderRequest) -> dict[str, Any]:
        if req.effort not in VALID_EFFORTS:  # "none" -> upstream HTTP 400 (SPEC 7.3)
            raise ValueError(f"invalid reasoning effort {req.effort!r}")
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "reasoning_effort": req.effort,
        }
        if req.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": f"{req.job}_output", "schema": req.json_schema, "strict": True},
            }
        return payload

    async def complete(self, req: ProviderRequest) -> ProviderResult:
        payload = self.build_payload(req)
        try:
            resp = await self._http.post("/chat/completions", json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"transport error: {type(exc).__name__}", retriable=True) from exc
        if resp.status_code != 200:
            retry_after = resp.headers.get("retry-after")
            raise ProviderError(
                f"HTTP {resp.status_code}",
                status=resp.status_code,
                retriable=resp.status_code in RETRIABLE_STATUS,
                retry_after_s=float(retry_after)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else None,
            )
        try:
            body = resp.json()
            text = body["choices"][0]["message"]["content"] or ""
            usage = body.get("usage") or {}
            return ProviderResult(
                text=text,
                tokens_in=int(usage.get("prompt_tokens", 0)),
                tokens_out=int(usage.get("completion_tokens", 0)),
                model=str(body.get("model", req.model)),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("malformed provider response", status=200, retriable=False) from exc
