# ADR-007: Deterministic local hashing embeddings for the filings index

- Status: accepted (2026-11-01), refines ADR-005
- Owner: stream B (research & LLM)

## Context
SPEC 7.5 embeds every filing chunk into pgvector and retrieves with vector + FTS fused by reciprocal rank.
SPEC 17.1 item 7 ("embedding model choice available via Meta API or open-weight alternative") is unverified and
there is no Meta key. CI must never call a network model, and tests must be byte-for-byte reproducible.

## Decision
Use a **signed feature-hashing embedding** (`app/modules/research/embedding.py`, version `hashing-v1`):
lower-cased unigrams and bigrams (stop words removed) hash with BLAKE2b into 1024 signed buckets, weighted by
`1 + ln(tf)` (bigrams x0.5), then L2-normalised. `doc_chunks.embedding` stays `vector(1024)` (migration 0002).

## Consequences
+ Zero dependencies beyond numpy, no model download, deterministic, ~20k chunks embed in seconds.
+ Schema, HNSW index and retrieval code are unchanged when a real model replaces it (`Embedder` protocol).
- It is lexical, not semantic: paraphrases without shared vocabulary are missed. Mitigation: hybrid retrieval
  (Postgres FTS with English stemming) + reciprocal-rank fusion + MMR/lexical rerank; an LLM reranker is
  pluggable (`research.retrieval.Reranker`).
- Collisions: 1024 buckets with signed hashing add noise for very long texts; chunks are ~800 tokens, which
  keeps the effect small. Not a security boundary.

## Migration path
Choose a model, set `EMBEDDING_DIM`, add a migration that re-types `doc_chunks.embedding` (or adds a second
column), re-embed with `python -m app.modules.research.cli reembed`, bump `EMBEDDING_VERSION`.
