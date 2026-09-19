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
MAX_CHARS = 2000


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
    print(f"{len(train_rows)} train pairs, {len(val)} held out for validation")

    model = SentenceTransformer(MODEL_ID)
    examples = [InputExample(texts=[r["a"], r["p"]]) for r in train_rows]
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
        if args.export_pairs:
            n = export_pairs(conn, Path(args.export_pairs))
            mb = Path(args.export_pairs).stat().st_size / 1048576
            print(f"exported {n:,} training pairs -> {args.export_pairs} ({mb:.1f} MB)")
        else:
            ap.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
