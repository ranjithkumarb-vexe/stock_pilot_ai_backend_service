"""Hybrid retrieval over filing chunks (SPEC 7.5 "Retrieval"; T-RAG-06).

vector (cosine, local hashing embedding) + Postgres FTS -> reciprocal-rank fusion -> top 20 -> rerank
(lexical + MMR by default; pluggable LLM reranker) -> top 6-8.

Guards enforced in SQL, not in Python: quarantined chunks never appear, and with `as_of` no chunk whose
`filed_at` is after that date is ever returned (look-ahead guard).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.core.db import Connection, vec_literal
from app.modules.research.embedding import Embedder, HashingEmbedder, cosine, tokenize

RRF_K = 60
CANDIDATES = 40
FUSED_TOP = 20
FINAL_TOP = 7

_VECTOR_SQL = """
with cand as materialized (
    select id, embedding from doc_chunks
     where symbol = $1 and not quarantined and embedding is not null
       and ($2::date is null or filed_at::date <= $2::date)
       and ($3::text[] is null or item = any($3::text[]))
       and ($4::text[] is null or form = any($4::text[]))
       and ($7::text[] is null or accession_no = any($7::text[]))
)
select id, embedding <=> $5::vector as dist from cand order by dist limit $6
"""

_FTS_SQL = """
select id, ts_rank_cd(tsv, q) as rank
  from doc_chunks, to_tsquery('english', $5) q
 where symbol = $1 and not quarantined and tsv @@ q
   and ($2::date is null or filed_at::date <= $2::date)
   and ($3::text[] is null or item = any($3::text[]))
   and ($4::text[] is null or form = any($4::text[]))
   and ($7::text[] is null or accession_no = any($7::text[]))
 order by rank desc limit $6
"""

_FETCH_SQL = """
select id, document_id, symbol, form, accession_no, filed_at, item, period, anchor, chunk_no, text
  from doc_chunks where id = any($1::uuid[]) and not quarantined
"""


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    id: str
    document_id: str
    symbol: str
    form: str | None
    accession_no: str | None
    filed_at: datetime | None
    item: str | None
    period: str | None
    anchor: str | None
    chunk_no: int
    text: str
    score: float = 0.0


class Reranker(Protocol):
    async def rerank(self, query: str, chunks: Sequence[RetrievedChunk], k: int) -> list[RetrievedChunk]: ...


def fts_query(query: str) -> str | None:
    """OR-query of stemmed-by-Postgres terms: `to_tsquery('english', 'revenue | growth | ...')`."""
    terms = [t for t in dict.fromkeys(tokenize(query)) if re.fullmatch(r"[a-z0-9]+", t)]
    return " | ".join(terms[:24]) if terms else None


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for pos, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + pos + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


class LexicalMmrReranker:
    """Local reranker: BM25-ish query-term coverage blended with fusion score, then MMR for diversity."""

    def __init__(self, embedder: Embedder | None = None, mmr_lambda: float = 0.75) -> None:
        self._embedder = embedder or HashingEmbedder()
        self._lambda = mmr_lambda

    async def rerank(self, query: str, chunks: Sequence[RetrievedChunk], k: int) -> list[RetrievedChunk]:
        q_terms = set(tokenize(query))
        if not chunks:
            return []
        rel: dict[str, float] = {}
        vecs = {c.id: self._embedder.embed(c.text) for c in chunks}
        for c in chunks:
            toks = tokenize(c.text)
            if not toks:
                rel[c.id] = 0.0
                continue
            hits = sum(1 for t in toks if t in q_terms)
            coverage = len(q_terms & set(toks)) / (len(q_terms) or 1)
            density = hits / math.sqrt(len(toks))
            rel[c.id] = 0.6 * coverage + 0.25 * min(1.0, density) + 0.15 * min(1.0, c.score * RRF_K / 2)
        selected: list[RetrievedChunk] = []
        remaining = list(chunks)
        while remaining and len(selected) < k:

            def mmr(c: RetrievedChunk) -> float:
                red = max((cosine(vecs[c.id], vecs[s.id]) for s in selected), default=0.0)
                return self._lambda * rel[c.id] - (1 - self._lambda) * red

            best = max(remaining, key=lambda c: (mmr(c), c.id))
            selected.append(best)
            remaining.remove(best)
        return selected


async def hybrid_search(
    conn: Connection,
    symbol: str,
    query: str,
    *,
    as_of: date | None = None,
    items: Sequence[str] | None = None,
    forms: Sequence[str] | None = None,
    accession_nos: Sequence[str] | None = None,
    k: int = FINAL_TOP,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> list[RetrievedChunk]:
    embedder = embedder or HashingEmbedder()
    item_arg = list(items) if items else None
    form_arg = list(forms) if forms else None
    acc_arg = list(accession_nos) if accession_nos else None
    qvec = vec_literal(embedder.embed(query))
    vrows = await conn.fetch(_VECTOR_SQL, symbol, as_of, item_arg, form_arg, qvec, CANDIDATES, acc_arg)
    rankings: list[list[str]] = [[str(r["id"]) for r in vrows]]
    tsq = fts_query(query)
    if tsq:
        frows = await conn.fetch(_FTS_SQL, symbol, as_of, item_arg, form_arg, tsq, CANDIDATES, acc_arg)
        rankings.append([str(r["id"]) for r in frows])
    fused = rrf_fuse(rankings)[:FUSED_TOP]
    if not fused:
        return []
    rows = await conn.fetch(_FETCH_SQL, [cid for cid, _ in fused])
    by_id = {str(r["id"]): r for r in rows}
    cands = [
        RetrievedChunk(
            id=cid,
            document_id=str(by_id[cid]["document_id"]),
            symbol=by_id[cid]["symbol"],
            form=by_id[cid]["form"],
            accession_no=by_id[cid]["accession_no"],
            filed_at=by_id[cid]["filed_at"],
            item=by_id[cid]["item"],
            period=by_id[cid]["period"],
            anchor=by_id[cid]["anchor"],
            chunk_no=by_id[cid]["chunk_no"],
            text=by_id[cid]["text"],
            score=score,
        )
        for cid, score in fused
        if cid in by_id
    ]
    return await (reranker or LexicalMmrReranker(embedder)).rerank(query, cands, k)
