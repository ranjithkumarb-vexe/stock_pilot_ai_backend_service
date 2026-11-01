"""Traces for blocked injections (FR-16): one system trace per filing chunk the injection scanner quarantined.

`record_injection_block` is the public hook (stream B's ingest can call it right after it quarantines a chunk -
see the change request in HANDOFF.md); `backfill_quarantine_traces` creates the missing traces for chunks that
were quarantined before the hook existed or by a CLI run. Traces never contain chunk text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.clock import Clock
from app.core.db import Connection, system_tx
from app.core.logging import get_logger
from app.modules.llm.trace import TraceRecorder
from app.modules.research.injection import HeuristicScanner, ScanResult

log = get_logger("app.audit.quarantine")
_SCANNER = HeuristicScanner()


def trace_id_for(chunk_id: object) -> str:
    return f"quarantine:{chunk_id}"


@dataclass(frozen=True, slots=True)
class BlockedChunk:
    chunk_id: str
    symbol: str
    form: str | None
    item: str | None
    accession_no: str | None


def _record(chunk: BlockedChunk, scan: ScanResult, scan_ms: int, clock: Clock | None = None) -> TraceRecorder:
    form = chunk.form or "filing"
    rec = TraceRecorder(
        f"ingest:injection:{chunk.symbol}:{form}",
        user_id=None,
        trace_id=trace_id_for(chunk.chunk_id),
        clock=clock,
    )
    rec.add(
        "Fetch filing chunk from allowlisted host sec.gov", 0, "ok",
        {"summary": f"1 chunk ({form}{' Item ' + chunk.item if chunk.item else ''})", "accession_no": chunk.accession_no},
    )  # fmt: skip
    rec.add(
        "Injection scan (heuristic; PromptGuard 2 hook)", scan_ms, "blocked",
        {"summary": f"Injection score {scan.score:.2f}", "reasons": ",".join(scan.reasons)},
    )  # fmt: skip
    rec.add("Chunk quarantined; excluded from retrieval and from every model prompt", 0, "ok", {"summary": "Not sent to model"})  # fmt: skip
    rec.set_outcome("blocked")
    return rec


async def record_injection_block(chunk: BlockedChunk, scan: ScanResult, scan_ms: int = 0) -> str | None:
    """Persist the trace for one quarantined chunk; returns the `traces.id` (None if tracing is unavailable)."""
    rid = await _record(chunk, scan, scan_ms).flush()
    return None if rid is None else str(rid)


async def backfill_quarantine_traces(conn: Connection | None = None, limit: int = 200) -> int:
    """Create the missing traces (idempotent: keyed by `quarantine:<chunk id>`). Returns how many were created."""

    async def run(c: Connection) -> list[Any]:
        return list(
            await c.fetch(
                "select k.id, k.symbol, k.form, k.item, k.accession_no, k.text from doc_chunks k "
                "where k.quarantined and not exists (select 1 from traces t where t.user_id is null and t.trace_id = 'quarantine:' || k.id::text) "
                "order by k.id limit $1",
                limit,
            )
        )

    if conn is not None:
        rows = await run(conn)
    else:
        async with system_tx() as c:
            rows = await run(c)
    made = 0
    for r in rows:
        t0 = time.perf_counter()
        scan = _SCANNER.scan(r["text"])
        ms = int((time.perf_counter() - t0) * 1000)
        chunk = BlockedChunk(str(r["id"]), r["symbol"], r["form"], r["item"], r["accession_no"])
        if await _record(chunk, scan, ms).flush() is not None:
            made += 1
    if made:
        log.info("quarantine traces created", extra={"count": made})
    return made
