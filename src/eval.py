"""Stage 2b: recall@k and MRR harness.

    from src.eval import evaluate
    from src.testset import load
    evaluate(my_retriever, load("test"))

    python -m src.eval    # sanity-check the harness itself

A retriever takes (query_text, before_date, n) and returns issue numbers, best
first. Ignoring before_date means answering with issues that did not exist yet --
it scores well here and is useless in production.
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
    """Fetch every query issue in one round trip, not one per pair."""
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

    Returns recall@k for each k, plus mrr, n, skipped and latency percentiles.

    Each query has exactly one right answer, so recall@k is a hit rate (and
    recall@1 is also precision@1). MRR is truncated at max(ks): anything ranked
    below the window scores 0, so only compare MRR at the same depth.
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
            skipped += 1  # no such issue to build a query from
            continue
        query_text, created_at = row

        t0 = time.perf_counter()
        ranked = retriever(query_text, created_at, depth)
        latencies.append((time.perf_counter() - t0) * 1000)

        # The query issue is never a valid answer. A time-filter bug should
        # look like a bad score, not a suspiciously good one.
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
    """Always returns the right answer first, so it must score exactly 1.0.

    A unit test of the metric, not a baseline. If this is not 1.0 the harness is
    broken and every later number is noise.
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
    """Picks at random from issues predating the query, to establish the floor.

    Makes a real retriever's score meaningful instead of just "better than
    nothing".
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
