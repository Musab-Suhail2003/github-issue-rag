"""Stage 3: encode issues into 384-dim vectors.

Three modes, because the encoding runs on Colab (GPU) while the database lives
on the laptop:

    python -m src.embed --export-texts texts.jsonl.gz    # laptop: dump what is missing
    python -m src.embed --encode texts.jsonl.gz -o out/  # Colab: GPU encode -> shards
    python -m src.embed --import-vectors out/            # laptop: load into MariaDB

`sentence_transformers` and `torch` are imported **inside** the encode path only.
Export and import must run on a machine with neither installed, which is exactly
the laptop this project develops on.

Checkpointing is by issue number, in both directions:
  * export emits only issues with no row in `embeddings` for this model, so a
    re-run after a partial import ships only the remainder;
  * encode writes numbered shards as it goes, so a reclaimed Colab runtime costs
    one shard rather than the whole run.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

import numpy as np

# NOTE: `src.db` is imported inside the functions that need it, not here. The
# --encode mode runs on a bare Colab runtime with no PyMySQL and no .env, and a
# module-level import would make the GPU step depend on the database layer for
# no reason. This file is uploadable to Colab as-is.

# The Hub id and the short key stored in the DB. The key must be ASCII and <= 48
# chars -- see the vector-index primary-key limit in schema.sql.
MODEL_ID = "BAAI/bge-small-en-v1.5"
MODEL_KEY = "bge-small-en-v1.5"
DIM = 384

# bge-small truncates at 512 tokens (~2k chars of English). Shipping more to
# Colab would be bandwidth the model discards unread. db.issue_text() is the
# shared composition; this is only where it gets cut.
MAX_CHARS = 2000

SHARD_SIZE = 10_000


def export_texts(conn, path: Path, limit: int | None = None) -> int:
    """Write {"n": issue_number, "t": text} JSONL.gz for issues lacking a vector."""
    from src import db  # noqa: PLC0415

    sql = """
        SELECT i.number, i.title, i.body
        FROM issues i
        LEFT JOIN embeddings e
          ON e.issue_number = i.number AND e.model_name = %s
        WHERE e.issue_number IS NULL
        ORDER BY i.number
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    written = 0
    with db.cursor(conn) as cur:
        cur.execute(sql, (MODEL_KEY,))
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for number, title, body in rows:
                    text = db.issue_text(title, body, max_chars=MAX_CHARS)
                    if not text.strip():
                        # A handful of issues are titled with a single invisible
                        # character and have no body. Encoding whitespace yields a
                        # meaningless vector; skip and let them be unretrievable.
                        continue
                    fh.write(json.dumps({"n": number, "t": text}) + "\n")
                    written += 1
    return written


def encode(texts_path: Path, out_dir: Path, batch_size: int = 64) -> None:
    """GPU/CPU encode. The only mode that needs torch."""
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    out_dir.mkdir(parents=True, exist_ok=True)
    done = {int(p.stem.split("_")[1]) for p in out_dir.glob("ids_*.npy")}
    if done:
        print(f"resuming: {len(done)} shard(s) already present", file=sys.stderr)

    model = SentenceTransformer(MODEL_ID)
    print(f"{MODEL_ID} loaded, device={model.device}, "
          f"max_seq_length={model.max_seq_length}", file=sys.stderr)

    with gzip.open(texts_path, "rt", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh]
    print(f"{len(records):,} texts to encode", file=sys.stderr)

    for shard_idx in range(0, len(records), SHARD_SIZE):
        shard_no = shard_idx // SHARD_SIZE
        if shard_no in done:
            continue
        chunk = records[shard_idx:shard_idx + SHARD_SIZE]
        vectors = model.encode(
            [r["t"] for r in chunk],
            batch_size=batch_size,
            # Retrieval here is symmetric -- both sides are issue text. bge's
            # "Represent this sentence for searching relevant passages:" prefix
            # is for short-query-against-long-passage and would hurt here.
            normalize_embeddings=True,  # makes cosine similarity a plain dot product
            show_progress_bar=True,
            convert_to_numpy=True,
        ).astype(np.float32)
        np.save(out_dir / f"emb_{shard_no:05d}.npy", vectors)
        np.save(out_dir / f"ids_{shard_no:05d}.npy",
                np.array([r["n"] for r in chunk], dtype=np.int64))
        print(f"  shard {shard_no}: {len(chunk):,} vectors", file=sys.stderr)


def import_vectors(conn, out_dir: Path, chunk: int = 500) -> int:
    """Load shards into MariaDB.

    Vectors are sent as raw little-endian float32 bytes. MariaDB's VECTOR type
    is exactly that on the wire, so this avoids VEC_FromText() and the ~4KB of
    text-formatted floats per row it would otherwise parse.
    """
    from src import db  # noqa: PLC0415

    shards = sorted(out_dir.glob("ids_*.npy"))
    if not shards:
        raise SystemExit(f"no shards in {out_dir}")
    total = 0
    for ids_path in shards:
        emb_path = ids_path.with_name(ids_path.name.replace("ids_", "emb_"))
        ids = np.load(ids_path)
        vecs = np.load(emb_path).astype("<f4")
        if len(ids) != len(vecs):
            raise SystemExit(f"{ids_path.name}: {len(ids)} ids vs {len(vecs)} vectors")
        if vecs.shape[1] != DIM:
            raise SystemExit(f"{emb_path.name}: dim {vecs.shape[1]}, expected {DIM}")
        rows = [(int(n), MODEL_KEY, v.tobytes()) for n, v in zip(ids, vecs)]
        with db.cursor(conn) as cur:
            for i in range(0, len(rows), chunk):
                block = rows[i:i + chunk]
                cur.execute(
                    "INSERT INTO embeddings (issue_number, model_name, vec) VALUES "
                    + ",".join(["(%s,%s,%s)"] * len(block))
                    + " ON DUPLICATE KEY UPDATE vec=VALUES(vec)",
                    [v for row in block for v in row],
                )
        conn.commit()
        total += len(rows)
        print(f"  {ids_path.name}: +{len(rows):,}  (total {total:,})")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Encode issues into vectors")
    ap.add_argument("--export-texts", metavar="PATH")
    ap.add_argument("--encode", metavar="TEXTS")
    ap.add_argument("--import-vectors", metavar="DIR")
    ap.add_argument("-o", "--out", default="shards", help="shard dir for --encode")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--limit", type=int, help="export only N issues (smoke test)")
    args = ap.parse_args()

    if args.encode:
        encode(Path(args.encode), Path(args.out), args.batch_size)
        return

    from src import db  # noqa: PLC0415

    conn = db.connect()
    try:
        if args.export_texts:
            n = export_texts(conn, Path(args.export_texts), args.limit)
            size = Path(args.export_texts).stat().st_size / 1048576
            print(f"exported {n:,} texts -> {args.export_texts} ({size:.1f} MB gzipped)")
        elif args.import_vectors:
            n = import_vectors(conn, Path(args.import_vectors))
            with db.cursor(conn) as cur:
                cur.execute("SELECT COUNT(*) FROM embeddings WHERE model_name=%s", (MODEL_KEY,))
                have = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM issues")
                want = cur.fetchone()[0]
            print(f"\nimported {n:,}. embeddings table: {have:,} / {want:,} issues")
        else:
            ap.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
