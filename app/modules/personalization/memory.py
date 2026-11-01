"""Memory layer (SPEC 10.4-10.5): auto-derived memories, Mem0-style ADD / UPDATE / NOOP / archive consolidation.

Provenance rule (T-SEC-05): a memory can only originate from the user's *own* actions - events they generated
(clicks, decisions, questions' topic tags) or messages they typed. There is deliberately no function here that
accepts document, filing, news or tool text, and `create_memory` refuses any other `origin`. Candidate texts
are assembled from enums and identifiers (tickers, section keys, reason chips), never from free text.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from app.core.db import Connection
from app.core.logging import get_logger
from app.modules.personalization.features import SECTION_TITLES, EventRow
from app.modules.safety.memory_guard import MemoryPoisoningError, ensure_memory_source_allowed

log = get_logger("app.personalization.memory")

Kind = Literal["semantic", "episodic", "procedural"]
ALLOWED_ORIGINS = frozenset({"user_event", "user_message", "onboarding"})
CONTRADICTION_STEP = 0.2
ARCHIVE_BELOW = 0.2
REASON_LABEL = {
    "too_risky": "too risky",
    "disagree": "disagree with thesis",
    "timing": "timing",
    "already_know": "already know",
}
_INJECTION_RE = re.compile(
    r"ignore (all |any )?(previous|prior|above)|disregard|system prompt|you (must|should) now|"
    r"as an ai|<\s*/?\s*(system|assistant|script)|https?://|BEGIN (SYSTEM|INSTRUCTIONS)",
    re.IGNORECASE,
)
MAX_TEXT = 300


class MemoryOriginError(ValueError):
    """A memory was requested from a source that is not the user's own action."""


def looks_like_instruction(text: str) -> bool:
    """Local guard (stream B's safety scanner may be layered on top): instruction-like or link-bearing text is
    never stored as a memory."""
    return bool(_INJECTION_RE.search(text))


@dataclass(frozen=True, slots=True)
class Candidate:
    key: str
    kind: Kind
    text: str
    confidence: float
    source: str
    event_ids: tuple[str, ...] = field(default=())


def _label(dim: str) -> str:
    return dim.split(":", 1)[-1].replace("_", " ")


def _uuids(ids: Iterable[str], limit: int = 10) -> list[UUID]:
    out: list[UUID] = []
    for i in ids:
        try:
            out.append(UUID(str(i)))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def extract_candidates(
    *,
    events: Sequence[EventRow],
    interest: Mapping[str, float],
    baseline: Mapping[str, float],
    betas: Mapping[str, Mapping[str, float]],
    pinned: Sequence[str],
) -> tuple[list[Candidate], set[str]]:
    """Returns (candidates, contradicted keys). Only the user's own events are inputs."""
    cands: list[Candidate] = []
    contradicted: set[str] = set()

    for dim, w in interest.items():
        key = f"semantic:interest:{dim}"
        b = baseline.get(dim, 0.0)
        if w >= 0.7 and w - b >= 0.1:
            cands.append(
                Candidate(
                    key,
                    "semantic",
                    f"Shows strong interest in {_label(dim)}.",
                    round(min(0.9, 0.5 + (w - b)), 2),
                    "Views and briefs",
                )
            )
        elif w < 0.4:
            contradicted.add(key)

    by_ticker: dict[str, list[EventRow]] = defaultdict(list)
    for ev in events:
        ticker = ev.entity.get("ticker")
        if ev.type == "proposal_rejected" and isinstance(ticker, str):
            by_ticker[ticker].append(ev)
    for ticker, evs in sorted(by_ticker.items()):
        last = max(evs, key=lambda e: e.ts)
        reason = REASON_LABEL.get(str(last.props.get("reason")))
        n = len(evs)
        text = f"Rejected {n} proposal{'s' if n != 1 else ''} for {ticker}" + (
            f", most recent reason: {reason}." if reason else "."
        )
        cands.append(
            Candidate(
                f"episodic:reject:{ticker}",
                "episodic",
                text,
                round(min(0.9, 0.5 + 0.1 * n), 2),
                "Proposal decisions",
                tuple(e.event_id for e in evs),
            )
        )

    scored = [(s, v) for s, v in betas.items() if v["alpha"] + v["beta"] - 2.0 >= 2.0]
    if scored:
        sec, v = max(scored, key=lambda kv: kv[1]["mean"])
        if v["mean"] >= 0.65 and sec in SECTION_TITLES:
            cands.append(
                Candidate(
                    "procedural:section_focus",
                    "procedural",
                    f"Spends the most time on {SECTION_TITLES[sec]}; show it early.",
                    round(min(0.9, 0.4 + 0.5 * v["mean"]), 2),
                    "Section dwell times",
                )
            )
    if pinned:
        cands.append(
            Candidate(
                "procedural:pinned_metrics",
                "procedural",
                f"Checks {', '.join(pinned)} repeatedly; show it first.",
                0.7,
                "Metric clicks",
            )
        )

    queries = [e for e in events if e.type == "chat_query"]
    topics: Counter[str] = Counter(t for e in queries for t in (e.props.get("topics") or []))
    if len(queries) >= 5 and topics:
        topic, n = topics.most_common(1)[0]
        if n / len(queries) >= 0.4:
            cands.append(
                Candidate(
                    f"semantic:topic:{topic}",
                    "semantic",
                    f"Often asks about {topic.replace('_', ' ')}.",
                    0.6,
                    "Questions asked",
                    tuple(e.event_id for e in queries),
                )
            )
    return cands, contradicted


async def _json_feature(conn: Connection, user_id: UUID, key: str) -> dict[str, Any]:
    value = await conn.fetchval(
        "select value from user_features where user_id = $1 and key = $2", user_id, key
    )
    return dict(value) if isinstance(value, dict) else {}


async def _put_feature(
    conn: Connection, user_id: UUID, key: str, value: dict[str, Any], now: datetime
) -> None:
    await conn.execute(
        "insert into user_features (user_id, key, value, confidence, updated_at) values ($1, $2, $3, 1, $4) "
        "on conflict (user_id, key) do update set value = excluded.value, updated_at = excluded.updated_at",
        user_id,
        key,
        value,
        now,
    )


async def consolidate(
    conn: Connection, user_id: UUID, candidates: Sequence[Candidate], contradicted: set[str], now: datetime
) -> dict[str, int]:
    """ADD / UPDATE / NOOP / archive. Explicit memories (no `mem_key`) are never touched. Keys the user deleted
    are tombstoned and not re-created. Contradiction lowers confidence by 0.2 at most once per day."""
    rows = await conn.fetch(
        "select id, mem_key, text, confidence, archived from memories where user_id = $1 and mem_key is not null",
        user_id,
    )
    existing = {r["mem_key"]: r for r in rows}
    tombstones = set((await _json_feature(conn, user_id, "memory_tombstones")).get("keys", []))
    last_hit: dict[str, str] = await _json_feature(conn, user_id, "memory_contradictions")
    stats = {"add": 0, "update": 0, "noop": 0, "archive": 0}

    for c in candidates:
        if c.key in tombstones or len(c.text) > MAX_TEXT or looks_like_instruction(c.text):
            continue
        row = existing.get(c.key)
        ids = _uuids(c.event_ids)
        if row is None:
            await conn.execute(
                "insert into memories (user_id, kind, text, source, source_event_ids, confidence, created_at, mem_key) "
                "values ($1, $2, $3, $4, $5, $6, $7, $8)",
                user_id, c.kind, c.text, c.source, ids, c.confidence, now, c.key,
            )  # fmt: skip
            stats["add"] += 1
        elif (
            row["text"] == c.text
            and abs(float(row["confidence"]) - c.confidence) < 0.05
            and not row["archived"]
        ):
            stats["noop"] += 1
        else:
            await conn.execute(
                "update memories set text = $3, confidence = $4, source = $5, source_event_ids = $6, archived = false, "
                "embedding = case when text = $3 then embedding else null end where user_id = $1 and id = $2",
                user_id, row["id"], c.text, c.confidence, c.source, ids,
            )  # fmt: skip
            stats["update"] += 1

    today = now.date().isoformat()
    produced = {c.key for c in candidates}
    for key in sorted(contradicted - produced):
        row = existing.get(key)
        if row is None or row["archived"] or last_hit.get(key) == today:
            continue
        new_conf = round(max(0.0, float(row["confidence"]) - CONTRADICTION_STEP), 4)
        archived = new_conf < ARCHIVE_BELOW
        await conn.execute(
            "update memories set confidence = $3, archived = $4 where user_id = $1 and id = $2",
            user_id, row["id"], new_conf, archived,
        )  # fmt: skip
        last_hit[key] = today
        stats["archive" if archived else "update"] += 1
    if last_hit:
        await _put_feature(conn, user_id, "memory_contradictions", last_hit, now)
    return stats


async def create_memory(
    conn: Connection,
    *,
    user_id: UUID,
    kind: Kind,
    text: str,
    origin: str,
    source: str = "User statement",
    confidence: float = 0.6,
    event_ids: Sequence[UUID] = (),
    now: datetime | None = None,
) -> UUID:
    """The only public way to store an explicit memory. Refuses anything that is not the user's own action."""
    if origin not in ALLOWED_ORIGINS:
        log.warning("memory refused", extra={"origin": origin[:24]})
        raise MemoryOriginError(f"memories can only come from the user's own actions, not {origin[:24]!r}")
    try:  # stream B's shared guard (same allow-list) as defence in depth
        ensure_memory_source_allowed(origin)
    except MemoryPoisoningError as exc:
        raise MemoryOriginError(str(exc)) from None
    clean = text.strip()
    if not clean or len(clean) > MAX_TEXT:
        raise ValueError("memory text must be 1-300 characters")
    if looks_like_instruction(clean):
        raise MemoryOriginError("instruction-like text is never stored as a memory")
    row = await conn.fetchval(
        "insert into memories (user_id, kind, text, source, source_event_ids, confidence, created_at) "
        "values ($1, $2, $3, $4, $5, $6, coalesce($7, now())) returning id",
        user_id, kind, clean, source, list(event_ids), confidence, now,
    )  # fmt: skip
    return UUID(str(row))
