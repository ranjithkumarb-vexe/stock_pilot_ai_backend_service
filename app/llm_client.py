"""SPEC name for the single LLM entry point (`llm_client.py`); the implementation lives in `app.modules.llm`."""

from app.modules.llm.client import LlmClient, build_client, enforce_policy
from app.modules.llm.trace import TraceRecorder, current_trace, trace_scope
from app.modules.llm.types import LlmRequest, LlmResponse, Message, PolicyViolationError

__all__ = [
    "LlmClient",
    "LlmRequest",
    "LlmResponse",
    "Message",
    "PolicyViolationError",
    "TraceRecorder",
    "build_client",
    "current_trace",
    "enforce_policy",
    "trace_scope",
]
