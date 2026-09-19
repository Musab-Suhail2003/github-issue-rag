"""Stage 5b: contrastive fine-tuning of the embedding model on duplicate pairs.

Same split as embed.py, for the same reason -- training runs on Colab, the
database is local:

    python -m src.finetune --export-pairs pairs.jsonl.gz   # laptop
    python -m src.finetune --train pairs.jsonl.gz -o out/  # Colab: GPU
    # then: embed.py --encode with the fine-tuned model, and import

Trains on the 2,012 *train* pairs only. The 504 test pairs are never seen, and
the split is chronological, so the model cannot learn from duplicates filed
after its own test queries.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

MODEL_ID = "BAAI/bge-small-en-v1.5"
MODEL_KEY_FT = "bge-small-ft-dup"     # ascii, <= 48 chars (see schema.sql)
MODEL_KEY_HN = "bge-small-ft-hardneg"
MAX_CHARS = 2000

# Negatives are taken from these ranks of the anchor's own neighbours. Ranks
# 1-4 are skipped: at that distance a neighbour is often a real duplicate that
# no maintainer happened to link, and training against it teaches the opposite
# of what we want.
NEG_RANK_LO, NEG_RANK_HI = 5, 60
NEGS_PER_ANCHOR = 3


def export_pairs(conn, path: Path) -> int:
    """Write {"a": duplicate_text, "p": canonical_text} for the train split.

    Uses cleaned text, since stage 6a measured that as better for dense
    retrieval -- training on boilerplate would teach the model to read GPU
    driver strings.
    """
    from src import db  # noqa: PLC0415
    from src.testset import load  # noqa: PLC0415

    pairs = load("train")
    wanted = {n for pair in pairs for n in pair}
    placeholders = ",".join(["%s"] * len(wanted))
    with db.cursor(conn) as cur:
        cur.execute(
            f"SELECT number, title, body FROM issues WHERE number IN ({placeholders})",
            list(wanted),
        )
        text = {n: db.issue_text(t, b, MAX_CHARS, clean=True)
                for n, t, b in cur.fetchall()}

    written = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for dup, canonical in pairs:
            a, p = text.get(dup, ""), text.get(canonical, "")
            # A pair where either side is empty teaches nothing and creates a
            # degenerate in-batch negative for everything else.
            if len(a.strip()) < 20 or len(p.strip()) < 20:
                continue
            fh.write(json.dumps({"a": a, "p": p}) + "\n")
            written += 1
    return written


def mine_negatives(conn, path: Path, model_key: str = MODEL_KEY_FT) -> int:
    """Write {"a":.., "p":.., "n":..} triplets with hard negatives.

    A hard negative is a near neighbour of the anchor that is not its canonical.
    Random negatives (what MNRL uses by default) are about unrelated features and
    teach nothing; these are same-area-different-issue, which is exactly where the
    model currently fails.
    """
    import gzip, json  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    from src import db  # noqa: PLC0415
    from src.testset import load  # noqa: PLC0415

    pairs = load("train")

    numbers, dates, blobs = [], [], []
    with db.cursor(conn) as cur:
        cur.execute(
            "SELECT e.issue_number, i.created_at, e.vec FROM embeddings e "
            "JOIN issues i ON i.number = e.issue_number "
            "WHERE e.model_name = %s ORDER BY i.created_at, e.issue_number",
            (model_key,),
        )
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for n, c, v in rows:
                numbers.append(n); dates.append(np.datetime64(c)); blobs.append(v)
    nums = np.array(numbers, dtype=np.int64)
    dts = np.array(dates, dtype="datetime64[s]")
    mat = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(numbers), -1)
    pos = {int(n): i for i, n in enumerate(nums)}

    # False-negative guard. If A and B are both duplicates of C then A and B are
    # duplicates of each other, so B must never be mined as a negative for A.
    all_pairs = load(None)
    by_canonical: dict[int, set[int]] = {}
    canonical_of: dict[int, int] = {}
    for d, c in all_pairs:
        by_canonical.setdefault(c, set()).add(d)
        canonical_of[d] = c

    with db.cursor(conn) as cur:
        cur.execute("SELECT number, title, body, created_at FROM issues")
        meta = {n: (t_, b_, c_) for n, t_, b_, c_ in cur.fetchall()}

    written = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for dup, canonical in pairs:
            if dup not in pos or dup not in meta or canonical not in meta:
                continue
            forbidden = {dup, canonical}
            forbidden |= by_canonical.get(canonical, set())
            if dup in canonical_of:
                forbidden.add(canonical_of[dup])

            created = meta[dup][2]
            cutoff = int(np.searchsorted(dts, np.datetime64(created), side="left"))
            if cutoff < NEG_RANK_HI:
                continue
            sims = mat[:cutoff] @ mat[pos[dup]]
            top = np.argpartition(-sims, NEG_RANK_HI)[:NEG_RANK_HI]
            top = top[np.argsort(-sims[top])]

            a_text = db.issue_text(*meta[dup][:2], MAX_CHARS, clean=True)
            p_text = db.issue_text(*meta[canonical][:2], MAX_CHARS, clean=True)
            if len(a_text.strip()) < 20 or len(p_text.strip()) < 20:
                continue

            picked = 0
            for idx in top[NEG_RANK_LO:]:
                cand = int(nums[idx])
                if cand in forbidden or cand not in meta:
                    continue
                n_text = db.issue_text(*meta[cand][:2], MAX_CHARS, clean=True)
                # Different issue numbers can carry identical text (the same
                # report filed twice). Numbers alone do not catch that.
                if len(n_text.strip()) < 20 or n_text == p_text or n_text == a_text:
                    continue
                fh.write(json.dumps({"a": a_text, "p": p_text, "n": n_text}) + "\n")
                written += 1
                picked += 1
                if picked >= NEGS_PER_ANCHOR:
                    break
    return written


def train(pairs_path: Path, out_dir: Path, epochs: int = 3,
          batch_size: int = 32, lr: float = 2e-5, holdout: int = 150) -> None:
    """Fine-tune with MultipleNegativesRankingLoss. Colab-side; needs torch."""
    import random  # noqa: PLC0415

    from sentence_transformers import (InputExample, SentenceTransformer,  # noqa: PLC0415
                                       losses)
    from sentence_transformers.evaluation import (  # noqa: PLC0415
        EmbeddingSimilarityEvaluator)
    from torch.utils.data import DataLoader  # noqa: PLC0415

    with gzip.open(pairs_path, "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    random.Random(0).shuffle(rows)
    val, train_rows = rows[:holdout], rows[holdout:]
    triplets = "n" in rows[0]
    print(f"{len(train_rows)} train examples ({'triplets' if triplets else 'pairs'}), "
          f"{len(val)} held out")

    model = SentenceTransformer(MODEL_ID)
    examples = [InputExample(texts=[r["a"], r["p"], r["n"]] if triplets
                             else [r["a"], r["p"]]) for r in train_rows]
    loader = DataLoader(examples, shuffle=True, batch_size=batch_size,
                        drop_last=True)

    # MultipleNegativesRankingLoss treats every other item in the batch as a
    # negative, so it needs only positive pairs -- which is all maintainers give
    # us. Larger batches mean more negatives and a harder, better task.
    loss = losses.MultipleNegativesRankingLoss(model)

    # Held-out pairs scored as similar(anchor, positive)=1. Crude, but it shows
    # whether the model is still improving or has started memorising 2k pairs.
    evaluator = EmbeddingSimilarityEvaluator(
        [r["a"] for r in val], [r["p"] for r in val], [1.0] * len(val),
        name="heldout",
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    model.fit(
        train_objectives=[(loader, loss)],
        evaluator=evaluator,
        epochs=epochs,
        warmup_steps=int(len(loader) * epochs * 0.1),
        optimizer_params={"lr": lr},
        output_path=str(out_dir),
        show_progress_bar=True,
    )
    print(f"saved to {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fine-tune the embedding model")
    ap.add_argument("--export-pairs", metavar="PATH")
    ap.add_argument("--mine-negatives", metavar="PATH",
                    help="export (anchor, positive, hard negative) triplets")
    ap.add_argument("--train", metavar="PAIRS")
    ap.add_argument("-o", "--out", default="ft-model")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-5)
    args = ap.parse_args()

    if args.train:
        train(Path(args.train), Path(args.out), args.epochs, args.batch_size, args.lr)
        return

    from src import db  # noqa: PLC0415

    conn = db.connect()
    try:
        if args.mine_negatives:
            n = mine_negatives(conn, Path(args.mine_negatives))
            mb = Path(args.mine_negatives).stat().st_size / 1048576
            print(f"mined {n:,} triplets -> {args.mine_negatives} ({mb:.1f} MB)")
        elif args.export_pairs:
            n = export_pairs(conn, Path(args.export_pairs))
            mb = Path(args.export_pairs).stat().st_size / 1048576
            print(f"exported {n:,} training pairs -> {args.export_pairs} ({mb:.1f} MB)")
        else:
            ap.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
