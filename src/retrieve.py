"""Stage 3: vector search.

Two retrieval paths, both worth having:

  * `VectorRetriever` -- brute-force cosine in numpy. Exact, and the time filter
    is free. This is what the eval and the Space use.
  * `MariaDBVectorRetriever` -- MariaDB's HNSW vector index. Approximate, and
    the time filter is the problem described below.

The encoder is **injected**, never constructed here. The Space passes a live
sentence-transformers model; the eval passes precomputed vectors. That keeps
torch out of the scoring path, which matters because this project's laptop has
no torch installed -- encoding happens on Colab.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from src import db
from src.embed import DIM, MAX_CHARS, MODEL_ID, MODEL_KEY


# ------------------------------------------------------------------ encoders

class PrecomputedEncoder:
    """Look up an already-computed vector by its exact text.

    Lets the eval score a retriever without loading a model. The key is the text
    truncated exactly as embed.py truncated it -- otherwise the query string and
    the embedded string differ and every lookup misses.
    """

    def __init__(self, text_to_vec: dict[str, np.ndarray]):
        self._map = text_to_vec

    @classmethod
    def for_issues(cls, conn, numbers: list[int],
                   model_name: str = MODEL_KEY) -> "PrecomputedEncoder":
        if not numbers:
            return cls({})
        placeholders = ",".join(["%s"] * len(numbers))
        with db.cursor(conn) as cur:
            cur.execute(
                f"SELECT i.title, i.body, e.vec FROM issues i "
                f"JOIN embeddings e ON e.issue_number = i.number "
                f"WHERE e.model_name = %s AND i.number IN ({placeholders})",
                [model_name, *numbers],
            )
            mapping = {
                db.issue_text(t, b, max_chars=MAX_CHARS): np.frombuffer(v, dtype="<f4")
                for t, b, v in cur.fetchall()
            }
        return cls(mapping)

    def __call__(self, text: str) -> np.ndarray | None:
        return self._map.get(text[:MAX_CHARS])


class ModelEncoder:
    """Live sentence-transformers encoding. Only this path needs torch."""

    def __init__(self, model_id: str = MODEL_ID):
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        self.model = SentenceTransformer(model_id)

    def __call__(self, text: str) -> np.ndarray:
        # normalize_embeddings=True and no bge query prefix -- must match
        # embed.py exactly, or query and document vectors live in different
        # spaces and the numbers measure that instead of the model.
        return self.model.encode(
            text[:MAX_CHARS], normalize_embeddings=True, convert_to_numpy=True
        ).astype(np.float32)


# ------------------------------------------------------------------ retrievers

class VectorRetriever:
    """Brute-force cosine over the whole corpus.

    At 84,942 x 384 float32 the matrix is ~124MB and one query is a single
    matmul -- a few milliseconds. Exact, no index to tune, and crucially the
    `before_date` filter costs nothing: vectors are held in created_at order, so
    the time filter is a slice, not a scan.
    """

    def __init__(self, conn, encoder, model_name: str = MODEL_KEY):
        self.encoder = encoder
        numbers, dates, blobs = [], [], []
        with db.cursor(conn) as cur:
            cur.execute(
                "SELECT e.issue_number, i.created_at, e.vec "
                "FROM embeddings e JOIN issues i ON i.number = e.issue_number "
                "WHERE e.model_name = %s ORDER BY i.created_at, e.issue_number",
                (model_name,),
            )
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for number, created, vec in rows:
                    numbers.append(number)
                    dates.append(np.datetime64(created))
                    blobs.append(vec)
        if not numbers:
            raise RuntimeError(f"no embeddings for model_name={model_name!r}")
        self.numbers = np.array(numbers, dtype=np.int64)
        self.dates = np.array(dates, dtype="datetime64[s]")
        self.matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(-1, DIM)

    def __len__(self) -> int:
        return len(self.numbers)

    def __call__(self, query_text: str, before_date: datetime, n: int) -> list[int]:
        q = self.encoder(query_text)
        if q is None:
            return []
        # Strictly before: an issue created at the same instant as the query
        # could not have informed it.
        cutoff = int(np.searchsorted(self.dates, np.datetime64(before_date), side="left"))
        if cutoff == 0:
            return []
        # Vectors are L2-normalised at encode time, so a dot product IS cosine
        # similarity -- no division, no norms recomputed per query.
        sims = self.matrix[:cutoff] @ q
        k = min(n, cutoff)
        # argpartition is O(N) vs O(N log N) for a full sort; we only need the
        # top k ordered, not all 85k.
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [int(self.numbers[i]) for i in top]


class MariaDBVectorRetriever:
    """HNSW path, via MariaDB's VECTOR INDEX.

    Kept because it is the production-shaped answer at scales where a 124MB
    matrix in RAM stops being reasonable -- and because the comparison is worth
    measuring rather than asserting.

    The catch, and it is the interesting part: MariaDB only uses the vector
    index for a bare `ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n`. Add
    `WHERE created_at < ?` and the index cannot serve the query, so it silently
    becomes a full scan -- no error, just slow. The workaround here is to
    over-fetch `n * overfetch` neighbours with the bare indexed query and filter
    by date afterwards, which is approximate in a second way: if enough of the
    nearest neighbours postdate the query, fewer than n survive.
    """

    def __init__(self, conn, encoder, model_name: str = MODEL_KEY, overfetch: int = 10):
        self.conn, self.encoder = conn, encoder
        self.model_name, self.overfetch = model_name, overfetch

    def __call__(self, query_text: str, before_date: datetime, n: int) -> list[int]:
        q = self.encoder(query_text)
        if q is None:
            return []
        with db.cursor(self.conn) as cur:
            cur.execute(
                "SELECT issue_number FROM embeddings "
                "WHERE model_name = %s "
                "ORDER BY VEC_DISTANCE_COSINE(vec, %s) LIMIT %s",
                (self.model_name, q.astype("<f4").tobytes(), n * self.overfetch),
            )
            candidates = [r[0] for r in cur.fetchall()]
        if not candidates:
            return []
        placeholders = ",".join(["%s"] * len(candidates))
        with db.cursor(self.conn) as cur:
            cur.execute(
                f"SELECT number FROM issues "
                f"WHERE number IN ({placeholders}) AND created_at < %s",
                [*candidates, before_date],
            )
            allowed = {r[0] for r in cur.fetchall()}
        return [c for c in candidates if c in allowed][:n]


def explain_index_usage(conn, model_name: str = MODEL_KEY) -> str:
    """Confirm the vector index is actually used. CLAUDE.md asks for this."""
    probe = np.zeros(DIM, dtype="<f4")
    probe[0] = 1.0
    with db.cursor(conn) as cur:
        cur.execute(
            "EXPLAIN SELECT issue_number FROM embeddings "
            "ORDER BY VEC_DISTANCE_COSINE(vec, %s) LIMIT 10",
            (probe.tobytes(),),
        )
        rows = cur.fetchall()
    return "\n".join(str(r) for r in rows)


if __name__ == "__main__":
    import time

    from src.eval import evaluate, format_result
    from src.testset import load

    pairs = load("test")
    conn = db.connect()
    try:
        t0 = time.time()
        encoder = PrecomputedEncoder.for_issues(conn, [d for d, _ in pairs])
        dense = VectorRetriever(conn, encoder)
        print(f"loaded {len(dense):,} vectors in {time.time() - t0:.1f}s "
              f"({dense.matrix.nbytes / 1048576:.0f}MB)\n")

        print(format_result("dense brute-force", evaluate(dense, pairs)))

        hnsw = MariaDBVectorRetriever(conn, encoder)
        print(format_result("dense HNSW (MariaDB)", evaluate(hnsw, pairs)))

        print("\nEXPLAIN on the bare indexed query:")
        print(explain_index_usage(conn))
    finally:
        conn.close()
