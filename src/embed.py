"""Stage 3: encode issues into 384-dim vectors.

Split into three modes because encoding runs on Colab while the database is
local:

    python -m src.embed --export-texts texts.jsonl.gz    # laptop: dump what is missing
    python -m src.embed --encode texts.jsonl.gz -o out/  # Colab: GPU encode -> shards
    python -m src.embed --import-vectors out/            # laptop: load into MariaDB

Resumable both ways: export skips issues that already have a vector, encode
writes numbered shards so a dropped Colab session loses one shard.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

import numpy as np

# src.db is imported inside functions, not here, so --encode runs on a bare
# Colab runtime with no PyMySQL and no .env. Upload this file to Colab as-is.

# MODEL_KEY goes in the DB and must be ASCII, <= 48 chars (see schema.sql).
MODEL_ID = "BAAI/bge-small-en-v1.5"
MODEL_KEY = "bge-small-en-v1.5"
# Same model, boilerplate stripped from the text. Stored under its own key so
# the stage 3 vectors survive for a like-for-like comparison.
MODEL_KEY_CLEAN = "bge-small-en-v1.5-clean"
DIM = 384

# bge-small reads 512 tokens (~2k chars). Anything longer is discarded by the
# model, so there is no point shipping it to Colab.
MAX_CHARS = 2000

SHARD_SIZE = 10_000


def export_texts(conn, path: Path, limit: int | None = None,
                 model_key: str = MODEL_KEY, clean: bool = False) -> int:
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
        cur.execute(sql, (model_key,))
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for number, title, body in rows:
                    text = db.issue_text(title, body, max_chars=MAX_CHARS, clean=clean)
                    if not text.strip():
                        # A few issues are a single invisible character with no
                        # body. A vector for whitespace is meaningless.
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
            # No bge query prefix: both sides are issue text, so retrieval is
            # symmetric. The prefix is for short-query-vs-long-passage.
            normalize_embeddings=True,  # lets cosine be a plain dot product
            show_progress_bar=True,
            convert_to_numpy=True,
        ).astype(np.float32)
        np.save(out_dir / f"emb_{shard_no:05d}.npy", vectors)
        np.save(out_dir / f"ids_{shard_no:05d}.npy",
                np.array([r["n"] for r in chunk], dtype=np.int64))
        print(f"  shard {shard_no}: {len(chunk):,} vectors", file=sys.stderr)


def import_vectors(conn, out_dir: Path, chunk: int = 500,
                   model_key: str = MODEL_KEY) -> int:
    """Load shards into MariaDB.

    Sends raw little-endian float32 bytes, which is MariaDB's VECTOR wire
    format -- avoids VEC_FromText() parsing ~4KB of text per row.
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
        rows = [(int(n), model_key, v.tobytes()) for n, v in zip(ids, vecs)]
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
    ap.add_argument("--clean", action="store_true",
                    help="strip template boilerplate; uses the -clean model key")
    args = ap.parse_args()

    if args.encode:
        encode(Path(args.encode), Path(args.out), args.batch_size)
        return

    from src import db  # noqa: PLC0415

    conn = db.connect()
    try:
        key = MODEL_KEY_CLEAN if args.clean else MODEL_KEY
        if args.export_texts:
            n = export_texts(conn, Path(args.export_texts), args.limit, key, args.clean)
            size = Path(args.export_texts).stat().st_size / 1048576
            print(f"exported {n:,} texts -> {args.export_texts} ({size:.1f} MB gzipped)")
        elif args.import_vectors:
            n = import_vectors(conn, Path(args.import_vectors), model_key=key)
            with db.cursor(conn) as cur:
                cur.execute("SELECT COUNT(*) FROM embeddings WHERE model_name=%s", (key,))
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
