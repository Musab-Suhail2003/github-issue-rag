"""Stage 2b: recall@k and MRR harness.

    from src.eval import evaluate
    from src.testset import load
    evaluate(my_retriever, load("test"))

The retriever contract, fixed by CLAUDE.md:

    retriever(query_text: str, before_date: datetime, n: int) -> list[int]

returning issue numbers ranked best-first. `before_date` is not optional and not
advisory: a retriever that ignores it will score well here and be worthless in
production, because it is answering with issues that did not exist when the
question was asked. That is look-ahead bias, the RAG equivalent of training on
the future.

Run directly to sanity-check the harness against two synthetic retrievers:

    python -m src.eval
"""

from __future__ import annotations

import random
import statistics
import time
from datetime import datetime
from typing import Callable

from src import db

Retriever = Callable[[str, datetime, int], list[int]]


def _query_rows(conn, numbers: list[int]) -> dict[int, tuple[str, datetime]]:
    """One query, not N. Returns {issue_number: (text, created_at)}."""
    if not numbers:
        return {}
    placeholders = ",".join(["%s"] * len(numbers))
    with db.cursor(conn) as cur:
        cur.execute(
            f"SELECT number, title, body, created_at FROM issues "
            f"WHERE number IN ({placeholders})",
            numbers,
        )
        return {n: (db.issue_text(t, b), c) for n, t, b, c in cur.fetchall()}


def evaluate(
    retriever: Retriever,
    test_pairs: list[tuple[int, int]],
    ks: tuple[int, ...] = (1, 5, 10),
    verbose: bool = False,
) -> dict:
    """Score a retriever against labelled (duplicate, canonical) pairs.

    Returns {'recall@1': .., 'recall@5': .., 'recall@10': .., 'mrr': ..} plus
    'n', 'skipped' and latency percentiles.

    recall@k here is per-query hit rate: each query has exactly one correct
    answer, so "recall@k" and "hit rate@k" coincide. Worth knowing, because with
    one relevant document per query, recall@1 is also precision@1 and a reviewer
    may ask.

    MRR is truncated at max(ks): a canonical ranked below the retrieved window
    contributes 0 rather than an unknown 1/rank. That makes it MRR@max(ks), and
    comparisons across stages are only valid at the same max(ks).
    """
    conn = db.connect()
    try:
        rows = _query_rows(conn, [d for d, _ in test_pairs])
    finally:
        conn.close()

    depth = max(ks)
    hits = {k: 0 for k in ks}
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    scored = skipped = 0

    for i, (dup, canonical) in enumerate(test_pairs):
        row = rows.get(dup)
        if row is None:
            skipped += 1  # duplicate not in the issues table; nothing to query with
            continue
        query_text, created_at = row

        t0 = time.perf_counter()
        ranked = retriever(query_text, created_at, depth)
        latencies.append((time.perf_counter() - t0) * 1000)

        # Defensive: the query issue itself is never a valid answer. A retriever
        # honouring before_date will already exclude it, but a bug here would
        # otherwise look like a good score.
        ranked = [r for r in ranked if r != dup][:depth]

        scored += 1
        if canonical in ranked:
            rank = ranked.index(canonical) + 1
            reciprocal_ranks.append(1.0 / rank)
            for k in ks:
                if rank <= k:
                    hits[k] += 1
        else:
            reciprocal_ranks.append(0.0)

        if verbose and (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(test_pairs)}")

    out: dict[str, float | int] = {f"recall@{k}": hits[k] / scored if scored else 0.0
                                   for k in ks}
    out["mrr"] = statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0
    out["n"] = scored
    out["skipped"] = skipped
    if latencies:
        out["latency_ms_p50"] = round(statistics.median(latencies), 1)
        out["latency_ms_p95"] = round(sorted(latencies)[int(len(latencies) * 0.95)], 1)
    return out


def format_result(name: str, result: dict) -> str:
    ks = [k for k in result if k.startswith("recall@")]
    parts = " ".join(f"{k} {result[k]:.4f}" for k in ks)
    lat = (f"  p50 {result['latency_ms_p50']}ms" if "latency_ms_p50" in result else "")
    return f"{name:<22} {parts}  mrr {result['mrr']:.4f}  (n={result['n']}){lat}"


# --------------------------------------------------------------- self-check

def _oracle(pairs: list[tuple[int, int]]) -> Retriever:
    """Returns the right answer first. Must score exactly 1.0 on every metric.

    This is not a baseline, it is a unit test of the metric: if recall@1 is not
    1.0 here, the harness is broken and every number produced later is noise.
    """
    answer = dict(pairs)
    lookup = {}

    def retrieve(query_text: str, before_date: datetime, n: int) -> list[int]:
        return [lookup[query_text]] if query_text in lookup else []

    conn = db.connect()
    try:
        rows = _query_rows(conn, [d for d, _ in pairs])
    finally:
        conn.close()
    for dup, (text, _) in rows.items():
        lookup[text] = answer[dup]
    return retrieve


def _random_retriever(seed: int = 0) -> Retriever:
    """Draws from issues predating the query. Establishes the floor.

    The floor is not zero -- it is roughly n/|corpus before the query| -- and
    knowing that number is what makes stage 3's result meaningful rather than
    just 'better than nothing'.
    """
    conn = db.connect()
    with db.cursor(conn) as cur:
        cur.execute("SELECT number, created_at FROM issues ORDER BY created_at")
        rows = cur.fetchall()
    conn.close()
    numbers = [r[0] for r in rows]
    dates = [r[1] for r in rows]
    rng = random.Random(seed)

    def retrieve(query_text: str, before_date: datetime, n: int) -> list[int]:
        import bisect
        cutoff = bisect.bisect_left(dates, before_date)
        if cutoff <= 0:
            return []
        return rng.sample(numbers[:cutoff], min(n, cutoff))

    return retrieve


if __name__ == "__main__":
    from src.testset import load

    pairs = load("test")
    print(f"test split: {len(pairs)} pairs\n")

    print(format_result("oracle (must be 1.0)", evaluate(_oracle(pairs), pairs)))
    print(format_result("random (floor)", evaluate(_random_retriever(), pairs)))
