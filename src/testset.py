"""Stage 2a: extract the labelled (duplicate -> canonical) test set.

Deliberately separate from eval.py. Extraction is regex over 208k comments and
produces a *frozen artifact* (testset.json, committed); evaluation reads that
artifact and runs many times. If extraction ran inside every eval, a tweak to
the regex battery would silently move the benchmark, and the stage-to-stage
numbers in NOTES.md would stop being comparable. Freezing it is the whole point.

    python -m src.testset            # rebuild testset.json
    python -m src.testset --stats    # report only, write nothing
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from src import db

OUT = Path(__file__).resolve().parent.parent / "testset.json"

# Measured in stage 0, validated against the full corpus in stage 1 (46.4%
# extraction). See NOTES.md for why bare "#123" references are excluded: roughly
# half are false, and a noisy label caps the measurable ceiling invisibly.
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
    """First pattern to match wins, scanning comments newest-first.

    Newest-first because a thread can speculate early and conclude late; the
    closing comment is the maintainer's actual verdict.
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
            # Either created before the corpus bound, or a PR, or deleted.
            # CLAUDE.md requires dropping these rather than guessing.
            counts["canonical_not_in_corpus"] += 1
            continue

        # The eval is time-aware: it only searches issues created BEFORE the
        # query. If the canonical was filed after the duplicate, no time-aware
        # retriever could ever return it, and scoring against it would depress
        # every stage's recall for a reason that has nothing to do with
        # retrieval quality. Maintainers do sometimes close the older issue as a
        # duplicate of the newer one, so this is real, not hypothetical.
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

    # Time-ordered split. Chronological rather than random on purpose: stage 5b
    # fine-tunes on duplicate pairs, and a random split would let it learn from
    # pairs that postdate its own test queries. Chronological also mirrors how
    # the system would actually be used -- train on history, answer new issues.
    pairs.sort(key=lambda p: (p["created_at"], p["duplicate"]))
    cut = int(len(pairs) * (1 - TEST_FRACTION))
    for i, p in enumerate(pairs):
        p["split"] = "train" if i < cut else "test"

    # How often is the canonical itself a duplicate? A -> B -> C chains are left
    # intact: B is a real issue in the corpus and retrieving it for query A is
    # what a maintainer marked as correct. Recorded because it is a judgement
    # call someone could reasonably challenge.
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
