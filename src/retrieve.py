"""Stage 3: vector search.

Two paths: `VectorRetriever` (exact brute-force numpy, used by the eval and the
Space) and `MariaDBVectorRetriever` (HNSW index, kept for comparison).

The encoder is passed in rather than built here, so scoring never imports torch.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from src import db
from src.embed import DIM, MAX_CHARS, MODEL_ID, MODEL_KEY


# ------------------------------------------------------------------ encoders

class PrecomputedEncoder:
    """Return a stored vector by its exact text.

    Lets the eval run without loading a model. Truncates the key the same way
    embed.py did, or every lookup misses.
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
    """Encode with the real model. The only class here that needs torch."""

    def __init__(self, model_id: str = MODEL_ID):
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        self.model = SentenceTransformer(model_id)

    def __call__(self, text: str) -> np.ndarray:
        # Must match embed.py exactly -- same normalisation, no query prefix --
        # or queries and documents end up in different spaces.
        return self.model.encode(
            text[:MAX_CHARS], normalize_embeddings=True, convert_to_numpy=True
        ).astype(np.float32)


# ------------------------------------------------------------------ retrievers

class VectorRetriever:
    """Exact cosine search over the whole corpus.

    The matrix is ~124MB, so a query is one matmul. Vectors are held in
    created_at order, which makes the before_date filter a slice.
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
        # Strictly before -- an issue filed at the same instant is not an answer.
        cutoff = int(np.searchsorted(self.dates, np.datetime64(before_date), side="left"))
        if cutoff == 0:
            return []
        # Vectors are L2-normalised, so the dot product is already cosine.
        sims = self.matrix[:cutoff] @ q
        k = min(n, cutoff)
        # argpartition finds the top k in O(N); sorting all 85k would be wasteful.
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [int(self.numbers[i]) for i in top]


class MariaDBVectorRetriever:
    """Search via MariaDB's HNSW vector index.

    MariaDB only uses the index for a bare ORDER BY VEC_DISTANCE_COSINE(...)
    LIMIT n. Adding WHERE created_at < ? silently turns it into a full scan, so
    this over-fetches n*overfetch neighbours and date-filters afterwards. That
    is approximate twice over: if enough neighbours postdate the query, fewer
    than n survive.
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
    """Check the index is really used and not silently skipped."""
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
    