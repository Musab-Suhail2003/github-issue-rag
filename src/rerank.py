"""Stage 5: cross-encoder reranking.

A bi-encoder embeds query and document separately, so document vectors are
precomputed -- that is what makes search over 85k issues fast. A cross-encoder
reads the pair together and is far more accurate and far too slow to run over the
corpus. Hence two stages: retrieve ~50 candidates, then reorder them.

Stage 4 measured the ceiling this works under: 57.9% of queries have the answer
in neither retriever's top 50, so reranking cannot move recall@10 much. Judge it
on recall@1 and MRR.

    python -m src.rerank                       # fast serving-size model
    python -m src.rerank --model BAAI/bge-reranker-base
"""

from __future__ import annotations

import argparse
from datetime import datetime

import numpy as np

from src import db

# Serving-size default: 22M params, runs on a 2 vCPU Space. CLAUDE.md's eval
# model (bge-reranker-v2-m3, 568M) is far too slow here -- that comparison is
# the accuracy/latency tradeoff the README is supposed to state.
FAST_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Cross-encoders take the pair in one 512-token window, so each side gets about
# half. Truncating here rather than in the model avoids silently dropping the
# candidate's text when the query is long.
PAIR_CHARS = 500   # ~183 tokens/side; 900 chars was 2x slower for no measured gain


class CrossEncoderReranker:
    """Wraps a retriever and reorders its top `depth` results."""

    def __init__(self, base, model_id: str = FAST_MODEL, depth: int = 50,
                 batch_size: int = 64):
        from sentence_transformers import CrossEncoder  # noqa: PLC0415

        self.base, self.depth, self.batch_size = base, depth, batch_size
        self.model = CrossEncoder(model_id, max_length=512)
        self._text: dict[int, str] = {}

    def load_texts(self, conn) -> None:
        """Cache issue text once; reranking touches thousands of candidates."""
        with db.cursor(conn) as cur:
            cur.execute("SELECT number, title, body FROM issues")
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for n, t, b in rows:
                    self._text[n] = db.issue_text(t, b, PAIR_CHARS, clean=True)

    def __call__(self, query_text: str, before_date: datetime, n: int) -> list[int]:
        candidates = self.base(query_text, before_date, self.depth)
        if not candidates:
            return []
        q = query_text[:PAIR_CHARS]
        pairs = [(q, self._text.get(c, "")) for c in candidates]
        scores = self.model.predict(pairs, batch_size=self.batch_size,
                                    show_progress_bar=False)
        order = np.argsort(-np.asarray(scores))
        return [candidates[i] for i in order[:n]]


def export_task(conn, path: Path, depth: int = 50) -> int:
    """Dump the reranking task so a GPU can score it.

    Writes one record per query with its candidate ids, plus a shared id->text
    map, so candidate texts are not repeated 50 times.
    """
    import gzip, json  # noqa: PLC0415

    from src.retrieve import PrecomputedEncoder, VectorRetriever  # noqa: PLC0415
    from src.testset import load  # noqa: PLC0415

    pairs = load("test")
    dups = [d for d, _ in pairs]
    enc = PrecomputedEncoder.for_issues(conn, dups, "bge-small-ft-dup")
    dense = VectorRetriever(conn, enc, "bge-small-ft-dup")

    texts: dict[int, str] = {}
    with db.cursor(conn) as cur:
        cur.execute("SELECT number, title, body FROM issues")
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for n, ti, bo in rows:
                texts[n] = db.issue_text(ti, bo, PAIR_CHARS, clean=True)

    records, needed = [], set()
    for dup, _ in pairs:
        if dup not in texts:
            continue
        with db.cursor(conn) as cur:
            cur.execute("SELECT title, body, created_at FROM issues WHERE number=%s", (dup,))
            ti, bo, created = cur.fetchone()
        cands = dense(db.issue_text(ti, bo), created, depth)
        records.append({"q": dup, "qt": db.issue_text(ti, bo, PAIR_CHARS, clean=True),
                        "c": cands})
        needed.update(cands)

    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"texts": {str(n): texts[n] for n in needed if n in texts}}) + "\n")
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return len(records)


if __name__ == "__main__":
    import time

    from src.eval import evaluate, format_result
    from src.retrieve import PrecomputedEncoder, VectorRetriever
    from src.testset import load

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=FAST_MODEL)
    ap.add_argument("--depth", type=int, default=50)
    ap.add_argument("--limit", type=int, help="score only N queries (smoke test)")
    ap.add_argument("--export-task", metavar="PATH", help="dump candidates for GPU scoring")
    args = ap.parse_args()

    if args.export_task:
        from pathlib import Path as _P
        conn = db.connect()
        n = export_task(conn, _P(args.export_task), args.depth)
        conn.close()
        mb = _P(args.export_task).stat().st_size / 1048576
        print(f"exported {n} queries x {args.depth} candidates -> {args.export_task} ({mb:.1f} MB)")
        raise SystemExit

    pairs = load("test")
    if args.limit:
        pairs = pairs[: args.limit]
    conn = db.connect()
    try:
        enc = PrecomputedEncoder.for_issues(conn, [d for d, _ in pairs],
                                            "bge-small-ft-dup")
        dense = VectorRetriever(conn, enc, "bge-small-ft-dup")
        print(format_result("fine-tuned dense (no rerank)", evaluate(dense, pairs)))

        t0 = time.time()
        rr = CrossEncoderReranker(dense, args.model, args.depth)
        rr.load_texts(conn)
        print(f"loaded {args.model} in {time.time() - t0:.0f}s")
        print(format_result(f"+ rerank {args.model.split('/')[-1]}",
                            evaluate(rr, pairs)))
    finally:
        conn.close()
