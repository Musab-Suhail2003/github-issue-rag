"""Stage 6: encode titles alone, so title and body can be weighted separately.

Runs locally -- titles average 13.6 tokens, so no GPU round trip is needed.
Stored under its own model_name, keyed on (issue_number, model_name) like every
other variant, so nothing existing is disturbed.
"""
import sys, time
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import db  # noqa: E402

MODEL_ID = "Musab6969/bge-small-vscode-dup-hardneg"
MODEL_KEY = "bge-small-ft-hn-title"
BATCH = 128

def main() -> None:
    from sentence_transformers import SentenceTransformer
    conn = db.connect()
    with db.cursor(conn) as cur:
        cur.execute("""SELECT i.number, i.title FROM issues i
                       LEFT JOIN embeddings e ON e.issue_number=i.number
                                             AND e.model_name=%s
                       WHERE e.issue_number IS NULL ORDER BY i.number""", (MODEL_KEY,))
        rows = cur.fetchall()
    print(f"{len(rows):,} titles to encode", flush=True)
    if not rows:
        return
    model = SentenceTransformer(MODEL_ID)
    model.max_seq_length = 96      # longest observed title was 98 tokens
    t0 = time.time()
    for i in range(0, len(rows), 2000):
        chunk = rows[i:i+2000]
        vecs = model.encode([t or "" for _, t in chunk], batch_size=BATCH,
                            normalize_embeddings=True, show_progress_bar=False,
                            convert_to_numpy=True).astype("<f4")
        db._bulk(conn, "embeddings", ("issue_number","model_name","vec"),
                 [(int(n), MODEL_KEY, v.tobytes()) for (n,_), v in zip(chunk, vecs)],
                 ("vec",))
        conn.commit()
        done = i + len(chunk)
        rate = done / (time.time()-t0)
        print(f"  {done:,}/{len(rows):,}  {rate:.0f}/s  "
              f"eta {(len(rows)-done)/rate/60:.0f}m", flush=True)
    conn.close()

if __name__ == "__main__":
    main()
