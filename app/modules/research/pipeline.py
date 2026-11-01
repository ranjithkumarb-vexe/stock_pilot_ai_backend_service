"""Pure document processing: HTML -> sections -> chunks -> injection scan -> embedding (no I/O).

Quarantined chunks (scanner flagged) are kept for audit but get no embedding and are never retrieved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.research.chunking import chunk_text
from app.modules.research.embedding import Embedder, HashingEmbedder
from app.modules.research.htmltext import TABLE_MARK, Table, parse_html
from app.modules.research.injection import Scanner, default_scanner
from app.modules.research.sections import TARGET_ITEMS, split_10k

MIN_SECTION_CHARS = 400  # shorter than this = "incorporated by reference" / extraction miss


@dataclass(slots=True)
class ProcessedChunk:
    item: str
    chunk_no: int
    text: str
    token_count: int
    anchor: str
    quarantined: bool
    reasons: tuple[str, ...] = ()
    embedding: list[float] | None = None


@dataclass(slots=True)
class ProcessedTable:
    item: str
    idx: int
    rows: list[list[str]]

    @property
    def preview(self) -> str:
        return " | ".join(c for c in (self.rows[0] if self.rows else []) if c)[:200]


@dataclass(slots=True)
class ProcessedDocument:
    chunks: list[ProcessedChunk] = field(default_factory=list)
    tables: list[ProcessedTable] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    thin_sections: list[str] = field(default_factory=list)

    @property
    def quarantined(self) -> int:
        return sum(1 for c in self.chunks if c.quarantined)


def _chunks_for(
    item: str,
    text: str,
    scanner: Scanner,
    embedder: Embedder,
    *,
    target_tokens: int | None = None,
) -> list[ProcessedChunk]:
    kwargs = {"target_tokens": target_tokens} if target_tokens else {}
    out: list[ProcessedChunk] = []
    for ch in chunk_text(text, **kwargs):
        verdict = scanner.scan(ch.text)
        out.append(
            ProcessedChunk(
                item=item,
                chunk_no=ch.chunk_no,
                text=ch.text,
                token_count=ch.token_count,
                anchor=f"item-{item.lower()}-{ch.chunk_no}",
                quarantined=verdict.flagged,
                reasons=verdict.reasons,
                embedding=None if verdict.flagged else embedder.embed(ch.text),
            )
        )
    return out


def process_10k_html(
    html: str, *, scanner: Scanner | None = None, embedder: Embedder | None = None
) -> ProcessedDocument:
    scanner = scanner or default_scanner()
    embedder = embedder or HashingEmbedder()
    parsed = parse_html(html)
    sections = split_10k(parsed.text)
    doc = ProcessedDocument()
    tables_by_idx: dict[int, Table] = {t.idx: t for t in parsed.tables}
    for key in TARGET_ITEMS:
        sec = sections.get(key)
        if sec is None:
            continue
        doc.sections_found.append(key)
        if sec.prose_chars < MIN_SECTION_CHARS:
            doc.thin_sections.append(key)
        doc.chunks.extend(_chunks_for(key, sec.text, scanner, embedder))
        for m in TABLE_MARK.finditer(sec.text):
            t = tables_by_idx.get(int(m.group(1)))
            if t is not None:
                doc.tables.append(ProcessedTable(item=key, idx=t.idx, rows=t.rows))
    return doc


def process_8k_html(
    html: str, *, scanner: Scanner | None = None, embedder: Embedder | None = None
) -> ProcessedDocument:
    scanner = scanner or default_scanner()
    embedder = embedder or HashingEmbedder()
    parsed = parse_html(html)
    doc = ProcessedDocument(sections_found=["8-K"])
    doc.chunks = _chunks_for("8-K", parsed.text, scanner, embedder)
    for t in parsed.tables:
        doc.tables.append(ProcessedTable(item="8-K", idx=t.idx, rows=t.rows))
    return doc


def process_plain_text(
    text: str, *, item: str = "8-K", scanner: Scanner | None = None, embedder: Embedder | None = None
) -> ProcessedDocument:
    """For tests/fixtures that already have text (e.g. the injection set)."""
    scanner = scanner or default_scanner()
    embedder = embedder or HashingEmbedder()
    doc = ProcessedDocument(sections_found=[item])
    doc.chunks = _chunks_for(item, text, scanner, embedder)
    return doc
