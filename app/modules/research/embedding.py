"""Deterministic local embedding: signed feature hashing of unigrams + bigrams into 1024 dimensions.

No model download, no network, byte-for-byte reproducible across machines (ADR-005, docs/ADR/007).
It is a lexical embedding: it finds passages that *share vocabulary* with the query, which is the right
trade-off while the real embedding model is unverified (SPEC 17.1 item 7). The retrieval layer fuses it with
Postgres FTS by reciprocal rank so exact terms and near-duplicates both score well. Swap in a real model by
implementing `Embedder` and keeping `dim == 1024` (or add a migration for a new dimension).
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

import numpy as np

EMBEDDING_DIM = 1024
EMBEDDING_VERSION = "hashing-v1"

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-']*")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "which",
        "will",
        "with",
        "would",
        "not",
        "also",
        "than",
        "other",
        "such",
        "may",
        "can",
    ]
)


class Embedder(Protocol):
    dim: int
    version: str

    def embed(self, text: str) -> list[float]: ...


@lru_cache(maxsize=200_000)
def _slot(token: str) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
    return h % EMBEDDING_DIM, 1.0 if (h >> 63) & 1 else -1.0


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


class HashingEmbedder:
    dim = EMBEDDING_DIM
    version = EMBEDDING_VERSION

    def embed(self, text: str) -> list[float]:
        tokens = tokenize(text)
        vec = np.zeros(self.dim, dtype=np.float64)
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        for t, c in counts.items():
            idx, sign = _slot(t)
            vec[idx] += sign * (1.0 + math.log(c))
        bigrams: dict[str, int] = {}
        for a, b in itertools.pairwise(tokens):
            key = f"{a}_{b}"
            bigrams[key] = bigrams.get(key, 0) + 1
        for t, c in bigrams.items():
            idx, sign = _slot(t)
            vec[idx] += 0.5 * sign * (1.0 + math.log(c))
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            vec[0] = 1.0  # empty text: any fixed unit vector keeps cosine distance defined
            return [float(x) for x in vec]
        return [float(x) for x in (vec / norm)]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    av, bv = np.asarray(a), np.asarray(b)
    denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
    return float(np.dot(av, bv) / denom) if denom else 0.0
