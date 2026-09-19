"""Stage 3: vector search.

Two paths: `VectorRetriever` (exact brute-force numpy, used by the eval and the
Space) and `MariaDBVectorRetriever` (HNSW index, kept for comparison).

The encoder is passed in rather than built here, so scoring never imports torch.
"""

from __future__ import annotations

import math
import re
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
        """Map the query text the eval will pass to that issue's stored vector.

        The key is always the RAW text, because that is what eval.py composes.
        The vector may have been built from cleaned text -- that is the point of
        the comparison, and it is still the correct vector for this issue.
        """
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


# ------------------------------------------------------------------ tokenizers

# vscode issues are full of identifiers -- github/issue_read, v1.113.0,
# src/vs/workbench/foo.ts. How we split them decides whether BM25 can match on
# them at all, so all three are measured rather than argued about.

_WORDS = re.compile(r"[a-z0-9]+")
_CHUNKS = re.compile(r"\S+")
_TRIM = "\"'`(),.:;!?[]{}<>*#|"


def tok_atomic(text: str) -> list[str]:
    """Split on whitespace only. Identifiers stay whole: `github/issue_read`."""
    return [w for w in (c.strip(_TRIM) for c in _CHUNKS.findall(text.lower())) if w]


def tok_words(text: str) -> list[str]:
    """Split on every non-alphanumeric: `github`, `issue`, `read`."""
    return _WORDS.findall(text.lower())


def tok_both(text: str) -> list[str]:
    """Emit the whole identifier and its parts, so either can match."""
    out = []
    for chunk in tok_atomic(text):
        out.append(chunk)
        parts = _WORDS.findall(chunk)
        if len(parts) > 1:
            out.extend(parts)
    return out


TOKENIZERS = {"atomic": tok_atomic, "words": tok_words, "both": tok_both}


class BM25Index:
    """Okapi BM25 over an inverted index with the document weights precomputed.

    Same formula as rank_bm25's BM25Okapi (k1=1.5, b=0.75, epsilon=0.25), and
    verified to match it to floating point. Written out because rank_bm25
    recomputes the length-normalisation term for all 85k documents on every
    query token, which costs ~2.7s for a 290-token issue. Everything except the
    idf lookup depends only on the document, so it is precomputed once and a
    query becomes a scatter-add over postings lists: ~10ms.
    """

    K1, B, EPSILON = 1.5, 0.75, 0.25

    def __init__(self, docs: list[list[str]]):
        n = len(docs)
        doc_len = np.array([len(d) for d in docs], dtype=np.float32)
        avgdl = float(doc_len.mean())

        df: dict[str, int] = {}
        tfs = []
        for tokens in docs:
            tf: dict[str, int] = {}
            for tok in tokens:
                tf[tok] = tf.get(tok, 0) + 1
            tfs.append(tf)
            for tok in tf:
                df[tok] = df.get(tok, 0) + 1

        # rank_bm25's idf can go negative for terms in most documents; it floors
        # those at epsilon * mean_idf rather than letting them subtract.
        idf = {w: math.log(n - f + 0.5) - math.log(f + 0.5) for w, f in df.items()}
        floor = self.EPSILON * (sum(idf.values()) / len(idf))
        for w, v in idf.items():
            if v < 0:
                idf[w] = floor

        # denom[d] is the part of the score that depends only on the document.
        denom = self.K1 * (1 - self.B + self.B * doc_len / avgdl)

        buckets: dict[str, list] = {w: [[], []] for w in df}
        for d, tf in enumerate(tfs):
            for tok, f in tf.items():
                w = idf[tok] * (f * (self.K1 + 1)) / (f + denom[d])
                buckets[tok][0].append(d)
                buckets[tok][1].append(w)
        self.postings = {
            w: (np.array(ids, dtype=np.int32), np.array(ws, dtype=np.float32))
            for w, (ids, ws) in buckets.items()
        }
        self.n = n

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float32)
        # Repeated query terms contribute repeatedly, exactly as rank_bm25 does,
        # so count them instead of iterating duplicates.
        counts: dict[str, int] = {}
        for tok in query:
            counts[tok] = counts.get(tok, 0) + 1
        for tok, c in counts.items():
            hit = self.postings.get(tok)
            if hit is not None:
                ids, ws = hit
                out[ids] += ws * c
        return out


class BM25Retriever:
    """Lexical search over the same text the dense side embeds.

    Indexes the truncated text, not the full body, so the comparison isolates
    the retrieval method rather than how much text each side was given.
    """

    def __init__(self, conn, tokenizer=tok_words, max_chars: int = MAX_CHARS,
                 clean: bool = False):
        numbers, dates, docs = [], [], []
        with db.cursor(conn) as cur:
            cur.execute("SELECT number, title, body, created_at FROM issues "
                        "ORDER BY created_at, number")
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for number, title, body, created in rows:
                    numbers.append(number)
                    dates.append(np.datetime64(created))
                    docs.append(tokenizer(db.issue_text(title, body, max_chars,
                                                        clean=clean)))
        self.tokenizer = tokenizer
        self.numbers = np.array(numbers, dtype=np.int64)
        self.dates = np.array(dates, dtype="datetime64[s]")
        self.index = BM25Index(docs)

    def __len__(self) -> int:
        return len(self.numbers)

    def __call__(self, query_text: str, before_date: datetime, n: int) -> list[int]:
        cutoff = int(np.searchsorted(self.dates, np.datetime64(before_date), side="left"))
        if cutoff == 0:
            return []
        scores = self.index.scores(self.tokenizer(query_text[:MAX_CHARS]))[:cutoff]
        k = min(n, cutoff)
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        # Drop zero-score docs: they share no query term and are not candidates.
        return [int(self.numbers[i]) for i in top if scores[i] > 0]


class HybridRetriever:
    """Dense + BM25 merged with reciprocal rank fusion.

    Each side returns `depth` candidates, not `n`. Fusing two top-10 lists would
    throw away most of what either found before they get a chance to agree.
    """

    def __init__(self, dense, bm25, k: int = 60, depth: int = 50):
        self.dense, self.bm25 = dense, bm25
        self.k, self.depth = k, depth

    def __call__(self, query_text: str, before_date: datetime, n: int) -> list[int]:
        from src.fusion import reciprocal_rank_fusion  # noqa: PLC0415

        lists = [self.dense(query_text, before_date, self.depth),
                 self.bm25(query_text, before_date, self.depth)]
        return [num for num, _ in reciprocal_rank_fusion(lists, k=self.k)[:n]]


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
    import sys

    from src.embed import MODEL_KEY_CLEAN
    from src.eval import evaluate, format_result
    from src.testset import load

    pairs = load("test")
    dups = [d for d, _ in pairs]
    conn = db.connect()
    try:
        for key in (MODEL_KEY, MODEL_KEY_CLEAN, "bge-small-ft-dup"):
            with db.cursor(conn) as cur:
                cur.execute("SELECT COUNT(*) FROM embeddings WHERE model_name=%s", (key,))
                have = cur.fetchone()[0]
            if not have:
                print(f"{key}: not embedded yet, skipping")
                continue
            enc = PrecomputedEncoder.for_issues(conn, dups, key)
            r = VectorRetriever(conn, enc, key)
            label = {MODEL_KEY: "raw text",
                     MODEL_KEY_CLEAN: "boilerplate stripped"}.get(key, "FINE-TUNED")
            print(format_result(f"dense ({label})", evaluate(r, pairs)))
            del r, enc

        if "--hybrid" in sys.argv:
            enc = PrecomputedEncoder.for_issues(conn, dups, MODEL_KEY_CLEAN)
            dense = VectorRetriever(conn, enc, MODEL_KEY_CLEAN)
            bm25 = BM25Retriever(conn, tok_atomic, clean=True)
            print(format_result("bm25 (clean corpus)", evaluate(bm25, pairs)))
            print(format_result("hybrid (clean)",
                                evaluate(HybridRetriever(dense, bm25), pairs)))
    finally:
        conn.close()
