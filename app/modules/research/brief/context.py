"""Retrieve supporting passages per section. Every passage gets a handle `sK` that claims cite."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from app.core.db import Connection
from app.modules.research.brief.catalog import SECTIONS
from app.modules.research.retrieval import Reranker, hybrid_search

PASSAGES_PER_SECTION = 6


@dataclass(slots=True)
class Passage:
    handle: str
    chunk_id: str
    document_id: str
    form: str | None
    item: str | None
    accession_no: str | None
    filed_at: datetime | None
    anchor: str | None
    text: str
    section: str


@dataclass(slots=True)
class EightK:
    handle: str
    accession_no: str
    title: str
    filed_at: date


@dataclass(slots=True)
class BriefContext:
    latest_10k: str | None = None
    latest_10k_filed: date | None = None
    passages: dict[str, list[Passage]] = field(default_factory=dict)  # section -> passages
    eightks: list[EightK] = field(default_factory=list)

    def all_passages(self) -> dict[str, Passage]:
        out: dict[str, Passage] = {}
        for lst in self.passages.values():
            for p in lst:
                out[p.handle] = p
        return out


async def gather_context(
    conn: Connection, symbol: str, as_of: date, *, reranker: Reranker | None = None, n_8k: int = 3
) -> BriefContext:
    ctx = BriefContext()
    row = await conn.fetchrow(
        "select accession_no, filed_at from raw_documents where symbol = $1 and form = '10-K' "
        "and filed_at::date <= $2 and chunk_count is not null order by filed_at desc limit 1",
        symbol,
        as_of,
    )
    if row is not None:
        ctx.latest_10k, ctx.latest_10k_filed = row["accession_no"], row["filed_at"].date()
    handles: dict[str, str] = {}  # chunk id -> handle

    def handle_for(chunk_id: str) -> str:
        if chunk_id not in handles:
            handles[chunk_id] = f"s{len(handles) + 1}"
        return handles[chunk_id]

    docs8k = await conn.fetch(
        "select id, accession_no, title, filed_at from raw_documents where symbol = $1 and form = '8-K' "
        "and filed_at::date <= $2 and chunk_count is not null order by filed_at desc limit $3",
        symbol,
        as_of,
        n_8k,
    )
    for key, meta in SECTIONS.items():
        if not meta.query:
            continue
        found: list[Passage] = []
        if ctx.latest_10k is not None:
            items = [i for i in meta.items if i != "8-K"]
            hits = await hybrid_search(
                conn, symbol, meta.query, as_of=as_of, items=items or None, forms=["10-K"],
                accession_nos=[ctx.latest_10k], k=PASSAGES_PER_SECTION, reranker=reranker,
            )  # fmt: skip
            for h in hits:
                found.append(
                    Passage(
                        handle_for(h.id),
                        h.id,
                        h.document_id,
                        h.form,
                        h.item,
                        h.accession_no,
                        h.filed_at,
                        h.anchor,
                        h.text,
                        key,
                    )
                )
        if key == "cat":
            for d in docs8k:
                c = await conn.fetchrow(
                    "select id, document_id, form, item, accession_no, filed_at, anchor, text from doc_chunks "
                    "where document_id = $1 and not quarantined order by chunk_no limit 1",
                    d["id"],
                )
                if c is not None:
                    found.append(
                        Passage(handle_for(str(c["id"])), str(c["id"]), str(c["document_id"]), c["form"], c["item"],
                                c["accession_no"], c["filed_at"], c["anchor"], c["text"], key)
                    )  # fmt: skip
                    ctx.eightks.append(
                        EightK(
                            handle_for(str(c["id"])),
                            d["accession_no"],
                            d["title"] or "8-K",
                            d["filed_at"].date(),
                        )
                    )
        ctx.passages[key] = found
    return ctx
