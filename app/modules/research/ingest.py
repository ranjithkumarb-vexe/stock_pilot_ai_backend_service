"""Filing ingest (BE-12): submissions -> raw document + provenance -> sections/chunks/tables -> DB.

Resumable and idempotent: a document whose `raw_documents.chunk_count` is set and whose `ingest_version`
matches is skipped; HTTP bodies are cached on disk by `EdgarClient`, so a crashed run costs no SEC requests.
Every document is written in one transaction, so a crash leaves either nothing or a complete document.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.db import Connection, vec_literal
from app.core.logging import get_logger
from app.modules.edgar.client import EdgarClient, EdgarError, submissions_url
from app.modules.edgar.ingest import TickerRef
from app.modules.research.embedding import EMBEDDING_VERSION, Embedder, HashingEmbedder
from app.modules.research.filings import FilingRef, parse_submissions, select_filings
from app.modules.research.injection import Scanner, default_scanner
from app.modules.research.pipeline import ProcessedDocument, process_8k_html, process_10k_html

log = get_logger("app.research.ingest")

INGEST_VERSION = f"v1+{EMBEDDING_VERSION}"
SUBMISSIONS_MAX_AGE_S = 3600


@dataclass(slots=True)
class DocResult:
    accession_no: str
    form: str
    status: str  # ingested | skipped | error
    chunks: int = 0
    quarantined: int = 0
    tables: int = 0
    sections: list[str] = field(default_factory=list)
    thin_sections: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class TickerResult:
    symbol: str
    documents: list[DocResult] = field(default_factory=list)
    error: str | None = None


async def store_document(
    conn: Connection,
    *,
    symbol: str,
    filing: FilingRef,
    sha256: str,
    storage_path: str | None,
    fetched_at: datetime,
    processed: ProcessedDocument,
) -> str:
    """Upsert provenance, replace chunks/tables, mark the document complete. Returns the document id."""
    filed_ts = datetime(filing.filed_at.year, filing.filed_at.month, filing.filed_at.day, tzinfo=UTC)
    doc_id = await conn.fetchval(
        """
        insert into raw_documents (symbol, form, accession_no, title, url, filed_at, period_of_report,
                                   fetched_at, sha256, storage_path)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        on conflict (symbol, accession_no) do update
           set sha256 = excluded.sha256, fetched_at = excluded.fetched_at, storage_path = excluded.storage_path,
               title = excluded.title
        returning id
        """,
        symbol,
        filing.form,
        filing.accession_no,
        filing.title,
        filing.url,
        filed_ts,
        filing.report_date,
        fetched_at,
        sha256,
        storage_path,
    )
    await conn.execute("delete from doc_chunks where document_id = $1", doc_id)
    await conn.execute("delete from doc_tables where document_id = $1", doc_id)
    period = f"FY{filing.report_date.year}" if filing.report_date and filing.form == "10-K" else None
    chunks = processed.chunks
    if chunks:
        await conn.execute(
            """
            insert into doc_chunks (document_id, symbol, form, accession_no, filed_at, item, period, anchor,
                                    chunk_no, text, token_count, quarantined, embedding)
            select $1, $2, $3, $4, $5, i, $6, a, n, t, k, q, case when e is null then null else e::vector end
              from unnest($7::text[], $8::text[], $9::int[], $10::text[], $11::int[], $12::bool[], $13::text[])
                   as u(i, a, n, t, k, q, e)
            """,
            doc_id,
            symbol,
            filing.form,
            filing.accession_no,
            filed_ts,
            period,
            [c.item for c in chunks],
            [c.anchor for c in chunks],
            [c.chunk_no for c in chunks],
            [c.text for c in chunks],
            [c.token_count for c in chunks],
            [c.quarantined for c in chunks],
            [vec_literal(c.embedding) if c.embedding is not None else None for c in chunks],
        )
    tables = processed.tables
    if tables:
        await conn.execute(
            """
            insert into doc_tables (document_id, symbol, form, accession_no, filed_at, item, idx, n_rows, n_cols, preview, rows)
            select $1, $2, $3, $4, $5, i, x, nr, nc, p, r::jsonb
              from unnest($6::text[], $7::int[], $8::int[], $9::int[], $10::text[], $11::text[]) as u(i, x, nr, nc, p, r)
            """,
            doc_id,
            symbol,
            filing.form,
            filing.accession_no,
            filed_ts,
            [t.item for t in tables],
            [t.idx for t in tables],
            [len(t.rows) for t in tables],
            [max((len(r) for r in t.rows), default=0) for t in tables],
            [t.preview for t in tables],
            [json.dumps(t.rows) for t in tables],
        )
    quarantined = processed.quarantined
    await conn.execute(
        "update raw_documents set chunk_count = $2, ingest_version = $3, parsed_at = now(), "
        "sections_found = $4, quarantined = false, quarantine_reason = $5 where id = $1",
        doc_id,
        len(chunks),
        INGEST_VERSION,
        processed.sections_found,
        f"{quarantined} chunk(s) quarantined" if quarantined else None,
    )
    return str(doc_id)


async def _already_ingested(conn: Connection, symbol: str, accession_no: str) -> bool:
    row = await conn.fetchrow(
        "select chunk_count, ingest_version from raw_documents where symbol = $1 and accession_no = $2",
        symbol,
        accession_no,
    )
    return bool(row and row["chunk_count"] is not None and row["ingest_version"] == INGEST_VERSION)


async def ingest_filing(
    conn: Connection,
    client: EdgarClient,
    symbol: str,
    filing: FilingRef,
    *,
    force: bool = False,
    scanner: Scanner | None = None,
    embedder: Embedder | None = None,
) -> DocResult:
    res = DocResult(accession_no=filing.accession_no, form=filing.form, status="skipped")
    if not force and await _already_ingested(conn, symbol, filing.accession_no):
        return res
    try:
        fetched = await client.get(filing.url, max_age_s=None)  # filings are immutable
    except EdgarError as exc:
        res.status, res.error = "error", f"{type(exc).__name__}: {exc}"
        return res
    html = fetched.text()
    fn = process_10k_html if filing.form == "10-K" else process_8k_html
    processed: ProcessedDocument = await asyncio.to_thread(
        fn, html, scanner=scanner or default_scanner(), embedder=embedder or HashingEmbedder()
    )
    await store_document(
        conn,
        symbol=symbol,
        filing=filing,
        sha256=hashlib.sha256(fetched.body).hexdigest(),
        storage_path=client.cache_path(filing.url),
        fetched_at=fetched.fetched_at,
        processed=processed,
    )
    res.status = "ingested"
    res.chunks = len(processed.chunks)
    res.quarantined = processed.quarantined
    res.tables = len(processed.tables)
    res.sections = processed.sections_found
    res.thin_sections = processed.thin_sections
    log.info(
        "filing ingested",
        extra={
            "symbol": symbol,
            "form": filing.form,
            "accession": filing.accession_no,
            "chunks": res.chunks,
            "quarantined": res.quarantined,
        },
    )
    return res


async def list_filings(client: EdgarClient, ref: TickerRef) -> list[FilingRef]:
    payload: dict[str, Any] = await client.get_json(submissions_url(ref.cik), max_age_s=SUBMISSIONS_MAX_AGE_S)
    return parse_submissions(payload, ref.cik)


async def ingest_ticker(
    conn: Connection,
    client: EdgarClient,
    ref: TickerRef,
    *,
    n_10k: int = 1,
    n_8k: int = 3,
    force: bool = False,
    scanner: Scanner | None = None,
    embedder: Embedder | None = None,
) -> TickerResult:
    out = TickerResult(symbol=ref.symbol)
    try:
        filings = select_filings(await list_filings(client, ref), n_10k=n_10k, n_8k=n_8k)
    except EdgarError as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    for f in filings:
        out.documents.append(
            await ingest_filing(conn, client, ref.symbol, f, force=force, scanner=scanner, embedder=embedder)
        )
    return out
