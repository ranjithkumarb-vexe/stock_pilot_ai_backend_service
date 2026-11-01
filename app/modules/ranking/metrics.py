"""Offline ranking metrics (SPEC 10.8): NDCG@k, MRR, intra-list diversity. Pure; reused by the persona simulator."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence


def dcg(relevances: Sequence[float], k: int) -> float:
    return float(sum((2.0**r - 1.0) / math.log2(i + 2) for i, r in enumerate(relevances[:k])))


def ndcg_at_k(
    ranked_relevances: Sequence[float], k: int = 5, pool_relevances: Sequence[float] | None = None
) -> float:
    """NDCG@k of a ranked list. The ideal order is taken from `pool_relevances` (everything that could have been
    shown) when given, else from the list itself. 1.0 when there is nothing relevant to find."""
    ideal = sorted(pool_relevances if pool_relevances is not None else ranked_relevances, reverse=True)
    best = dcg(ideal, k)
    return 1.0 if best == 0 else dcg(ranked_relevances, k) / best


def mrr(ranked_relevances: Sequence[float]) -> float:
    for i, r in enumerate(ranked_relevances):
        if r > 0:
            return 1.0 / (i + 1)
    return 0.0


def intra_list_diversity[T](items: Sequence[T], similarity: Callable[[T, T], float]) -> float:
    """1 - mean pairwise similarity (1 = fully diverse)."""
    n = len(items)
    if n < 2:
        return 1.0
    total = sum(similarity(items[i], items[j]) for i in range(n) for j in range(i + 1, n))
    return 1.0 - total / (n * (n - 1) / 2)
