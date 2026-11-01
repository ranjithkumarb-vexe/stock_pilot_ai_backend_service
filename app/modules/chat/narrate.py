"""Narration: the LLM may only rephrase the code-built draft (Standard mode, user data allowed).

The model output is accepted only if (1) every numeric token is in the ledger, (2) it passes the output
guardrail, (3) it keeps to the draft's citation markers and is not wildly longer. Otherwise - and whenever the
provider is unavailable, over budget or the MockProvider - the deterministic draft is returned unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from app.modules.chat.answers import Draft
from app.modules.llm.client import LlmClient
from app.modules.llm.prompts import load_prompt
from app.modules.llm.providers.mock import register_mock_handler
from app.modules.llm.trace import TraceRecorder
from app.modules.llm.types import LlmRequest, Message, ProviderRequest
from app.modules.research.injection import wrap_untrusted
from app.modules.safety import check_output

JOB = "chat_qa"
_MARKER = re.compile(r"\[(\d{1,2})\]")


def _mock_handler(req: ProviderRequest) -> str:
    """Offline stand-in: 'phrasing' is the draft itself, so mock/degraded answers are deterministic."""
    return str(req.hints.get("draft", ""))


register_mock_handler(JOB, _mock_handler)


@dataclass(slots=True)
class Narration:
    text: str
    used_model: bool
    note: str


def build_messages(question: str, draft: Draft) -> list[Message]:
    prompt = load_prompt("chat_qa")
    facts = "\n".join(f"{e.id}: {e.label} = {e.display}" for e in draft.ledger.entries.values()) or "(none)"
    passages = "\n".join(wrap_untrusted(cid, text) for cid, text in draft.passages)
    body = f"QUESTION:\n{question}\n\nFACTS:\n{facts}\n\nDRAFT ANSWER:\n{draft.text}\n"
    if passages:
        body += f"\nPASSAGES:\n{passages}\n"
    return [Message("system", prompt.text), Message("user", body)]


def verify(text: str, draft: Draft) -> str | None:
    """None when acceptable, else the reason it was rejected."""
    if not text.strip():
        return "empty"
    bad = draft.ledger.unverified(text)
    if bad:
        return f"unverified_numbers:{len(bad)}"
    if check_output(text).blocked:
        return "output_guardrail"
    allowed = set(_MARKER.findall(draft.text))
    if not set(_MARKER.findall(text)) <= allowed:
        return "invented_citation"
    if len(text) > 3 * len(draft.text) + 400:
        return "too_long"
    return None


async def narrate(
    llm: LlmClient, question: str, draft: Draft, *, user_id: UUID, rec: TraceRecorder
) -> Narration:
    prompt = load_prompt("chat_qa")
    resp = await llm.call(
        LlmRequest(
            job=JOB,
            messages=build_messages(question, draft),
            data_class="user",
            user_id=user_id,
            prompt_version=prompt.version,
            max_tokens=600,
            hints={"draft": draft.text},
        ),
        trace=rec,
    )
    if resp.degraded:
        return Narration(draft.text, False, f"degraded:{resp.degraded_reason}")
    reason = verify(resp.text, draft)
    if reason is not None:
        return Narration(draft.text, False, f"rejected:{reason}")
    return Narration(resp.text.strip(), resp.provider != "mock", "ok")
