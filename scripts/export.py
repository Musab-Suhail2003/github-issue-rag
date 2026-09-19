"""Stage 8: MariaDB -> serving artifacts.

The Space reads these four files and nothing else. No database, no GitHub API,
no secrets -- so nothing external can break the demo.

    python scripts/export.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.embed import MAX_CHARS  # noqa: E402

MODEL_KEY = "bge-small-ft-hardneg"   # stage 5c, the best retriever
SNIPPET_CHARS = 600                  # enough to render a result card
OUT = Path(__file__).resolve().parent.parent / "artifacts"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    conn = db.connect()

    # --- vectors, ordered by created_at so the time filter is a slice ---
    numbers, dates, blobs = [], [], []
    with db.cursor(conn) as cur:
        cur.execute(
            "SELECT e.issue_number, i.created_at, e.vec "
            "FROM embeddings e JOIN issues i ON i.number = e.issue_number "
            "WHERE e.model_name = %s ORDER BY i.created_at, e.issue_number",
            (MODEL_KEY,),
        )
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for n, c, v in rows:
                numbers.append(n); dates.append(c); blobs.append(v)

    matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(numbers), -1)
    np.save(OUT / "embeddings.npy", matrix)
    np.save(OUT / "issue_ids.npy", np.array(numbers, dtype=np.int64))
    print(f"embeddings.npy  {matrix.shape}  {matrix.nbytes/1048576:.0f} MB")

    # --- metadata for rendering results ---
    sq = OUT / "issues.sqlite"
    sq.unlink(missing_ok=True)
    out = sqlite3.connect(sq)
    out.execute("""CREATE TABLE issues (
        number INTEGER PRIMARY KEY, title TEXT, snippet TEXT, state TEXT,
        state_reason TEXT, url TEXT, created_at TEXT, labels TEXT)""")
    with db.cursor(conn) as cur:
        cur.execute("""SELECT i.number, i.title, i.body, i.state, i.state_reason,
                              i.url, i.created_at,
                              GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',')
                       FROM issues i
                       LEFT JOIN issue_labels l ON l.issue_number = i.number
                       GROUP BY i.number""")
        batch = []
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for n, t, b, st, sr, url, c, labels in rows:
                text = db.issue_text(t, b, SNIPPET_CHARS, clean=True)
                snippet = text[len(t or ""):].strip()[:SNIPPET_CHARS]
                batch.append((n, t, snippet, st, sr, url, str(c), labels or ""))
            out.executemany("INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)", batch)
            batch = []
    out.commit()
    out.execute("CREATE INDEX idx_created ON issues(created_at)")
    out.commit(); out.close()
    print(f"issues.sqlite   {sq.stat().st_size/1048576:.0f} MB")

    # --- manifest: the UI shows this so the snapshot reads as deliberate ---
    (OUT / "manifest.json").write_text(json.dumps({
        "model": MODEL_KEY,
        "base_model": "BAAI/bge-small-en-v1.5",
        "hub_model": "Musab6969/bge-small-vscode-dup-hardneg",
        "issues": len(numbers),
        "corpus_start": "2024-01-01",
        "snapshot_date": max(dates).strftime("%Y-%m-%d"),
        "exported_at": datetime.utcnow().strftime("%Y-%m-%d"),
        "recall_at_10": 0.3393,
        "mrr": 0.1941,
        "test_pairs": 504,
    }, indent=1))
    print(json.dumps(json.loads((OUT / "manifest.json").read_text()), indent=1))
    conn.close()


if __name__ == "__main__":
    main()
