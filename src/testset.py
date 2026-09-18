"""Stage 2a: extract the labelled (duplicate -> canonical) test set.

    python -m src.testset            # rebuild testset.json
    python -m src.testset --stats    # report only, write nothing

Kept separate from eval.py so the test set is a frozen file. If extraction ran
inside every eval, changing a regex would silently move the benchmark and the
numbers in NOTES.md would stop being comparable.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from src import db

OUT = Path(__file__).resolve().parent.parent / "testset.json"

# Bare "#123" references are excluded on purpose -- about half are false, and a
# noisy label would cap the measurable ceiling invisibly. See NOTES.md.
REF = r"\[?(?:#|https://github\.com/microsoft/vscode/issues/)(\d+)"
PATTERNS = [
    ("dup_of",     re.compile(r"\b(?:duplicate|dup)\s+of\s+" + REF, re.I)),
    ("dup_colon",  re.compile(r"^\s*/?(?:duplicate|dup)\b[:\s]+" + REF, re.I | re.M)),
    ("same_as",    re.compile(r"covering the same as\s+" + REF, re.I)),
    ("tracked_in", re.compile(r"\b(?:tracked|fixed|covered)\s+(?:in|by|here)\b[^.\n]{0,40}?" + REF, re.I)),
]

TEST_FRACTION = 0.20


def duplicate_ids(conn) -> list[int]:
    """Issues marked duplicate by either signal (label or native close reason)."""
    with db.cursor(conn) as cur:
        cur.execute("""
            SELECT DISTINCT i.number
            FROM issues i
            LEFT JOIN issue_labels l
              ON l.issue_number = i.number AND l.label = '*duplicate'
            WHERE i.state_reason = 'DUPLICATE' OR l.issue_number IS NOT NULL
        """)
        return [r[0] for r in cur.fetchall()]


def comments_by_issue(conn) -> dict[int, list[str]]:
    with db.cursor(conn) as cur:
        cur.execute("SELECT issue_number, body FROM comments ORDER BY issue_number, created_at")
        out: dict[int, list[str]] = collections.defaultdict(list)
        for num, body in cur.fetchall():
            out[num].append(body or "")
    return out


def extract(issue_number: int, bodies: list[str]) -> tuple[int, str] | None:
    """First matching pattern wins, scanning comments newest-first.

    Newest-first because threads speculate early and conclude late.
    """
    for name, rx in PATTERNS:
        for body in reversed(bodies):
            m = rx.search(body)
            if m:
                target = int(m.group(1))
                if target != issue_number:  # self-reference guard
                    return target, name
    return None


def build(conn) -> dict:
    dup_ids = duplicate_ids(conn)
    comments = comments_by_issue(conn)
    with db.cursor(conn) as cur:
        cur.execute("SELECT number, created_at FROM issues")
        created = dict(cur.fetchall())

    counts = collections.Counter()
    pattern_hits = collections.Counter()
    pairs = []

    for num in dup_ids:
        got = extract(num, comments.get(num, []))
        if not got:
            counts["no_canonical_in_text"] += 1
            continue
        target, pattern = got
        pattern_hits[pattern] += 1

        if target not in created:
            # Filed before the corpus bound, or a PR, or deleted. Drop it.
            counts["canonical_not_in_corpus"] += 1
            continue

        # Maintainers sometimes close the OLDER issue as a duplicate of the
        # newer one. A time-aware retriever can never return those, so keeping
        # them would depress every stage's recall for no good reason.
        if created[target] >= created[num]:
            counts["canonical_newer_than_query"] += 1
            continue

        counts["usable"] += 1
        pairs.append({
            "duplicate": num,
            "canonical": target,
            "pattern": pattern,
            "created_at": created[num].isoformat(),
        })

    # Chronological split, not random: stage 5b fine-tunes on these pairs, and a
    # random split would let it train on pairs postdating its own test queries.
    pairs.sort(key=lambda p: (p["created_at"], p["duplicate"]))
    cut = int(len(pairs) * (1 - TEST_FRACTION))
    for i, p in enumerate(pairs):
        p["split"] = "train" if i < cut else "test"

    # A -> B -> C chains are kept: B is a real issue and a maintainer marked it
    # correct for A. Counted here because it is a debatable call.
    dup_set = set(dup_ids)
    chained = sum(1 for p in pairs if p["canonical"] in dup_set)

    return {
        "pairs": pairs,
        "meta": {
            "duplicate_marked": len(dup_ids),
            "funnel": dict(counts),
            "pattern_hits": dict(pattern_hits),
            "chained_canonicals": chained,
            "train": cut,
            "test": len(pairs) - cut,
            "test_fraction": TEST_FRACTION,
            "split": "chronological",
        },
    }


def load(split: str | None = "test") -> list[tuple[int, int]]:
    """Return [(duplicate, canonical)] for eval.py. split=None returns all."""
    data = json.loads(OUT.read_text())
    return [(p["duplicate"], p["canonical"]) for p in data["pairs"]
            if split is None or p["split"] == split]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    conn = db.connect()
    data = build(conn)
    conn.close()
    meta = data["meta"]

    print(f"duplicate-marked issues: {meta['duplicate_marked']:,}\n")
    print("funnel:")
    total = meta["duplicate_marked"]
    for k in ("no_canonical_in_text", "canonical_not_in_corpus",
              "canonical_newer_than_query", "usable"):
        v = meta["funnel"].get(k, 0)
        print(f"  {k:28} {v:>6,}  {v/total:.1%}")
    print("\npattern hits:")
    for k, v in sorted(meta["pattern_hits"].items(), key=lambda x: -x[1]):
        print(f"  {k:28} {v:>6,}")
    print(f"\nchained canonicals (target is itself a duplicate): {meta['chained_canonicals']:,}")
    print(f"split ({meta['split']}): train={meta['train']:,}  test={meta['test']:,}")

    if not args.stats:
        OUT.write_text(json.dumps(data, indent=1))
        print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
