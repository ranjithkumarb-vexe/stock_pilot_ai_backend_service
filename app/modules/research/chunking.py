"""Chunking: ~800 tokens with 15% overlap on sentence boundaries (SPEC 7.5 step 5)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.modules.research.htmltext import TABLE_MARK

TARGET_TOKENS = 800
OVERLAP_FRACTION = 0.15
TOKENS_PER_WORD = 1.3  # rough English/BPE ratio; good enough to size chunks

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_WORD = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(_WORD.findall(text)) * TOKENS_PER_WORD)


@dataclass(frozen=True, slots=True)
class TextChunk:
    chunk_no: int
    text: str
    token_count: int


def _units(text: str, target: int) -> list[str]:
    """Sentence-ish units; any unit larger than the target is cut on word boundaries."""
    units: list[str] = []
    for para in (p.strip() for p in TABLE_MARK.sub("", text).split("\n")):
        if not para:
            continue
        for sent in _SENT_SPLIT.split(para):
            sent = sent.strip()
            if not sent:
                continue
            if estimate_tokens(sent) <= target:
                units.append(sent)
                continue
            words = sent.split()
            step = max(1, int(target / TOKENS_PER_WORD / 6))  # small pieces so overlap still works
            units.extend(" ".join(words[i : i + step]) for i in range(0, len(words), step))
    return units


def chunk_text(
    text: str, *, target_tokens: int = TARGET_TOKENS, overlap: float = OVERLAP_FRACTION
) -> list[TextChunk]:
    units = _units(text, target_tokens)
    if not units:
        return []
    sizes = [estimate_tokens(u) for u in units]
    chunks: list[TextChunk] = []
    start = 0
    while start < len(units):
        total = 0
        end = start
        while end < len(units) and (total + sizes[end] <= target_tokens or end == start):
            total += sizes[end]
            end += 1
        body = " ".join(units[start:end])
        chunks.append(TextChunk(chunk_no=len(chunks), text=body, token_count=total))
        if end >= len(units):
            break
        # next chunk starts so that ~overlap of this chunk is repeated (always makes progress)
        back = 0
        nxt = end
        while nxt - 1 > start and back + sizes[nxt - 1] <= total * overlap:
            nxt -= 1
            back += sizes[nxt]
        start = max(nxt, start + 1)
    return chunks
