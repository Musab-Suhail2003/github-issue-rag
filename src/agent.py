"""Stage 9: tool-calling triage agent.

Three tools over the retrieval stack built in stages 1-6:

    search_duplicates(text, n)   -> ranked existing issues
    fetch_issue(number)          -> one issue's details
    suggest_labels(text, n)      -> predicted labels

    python -m src.agent "terminal freezes on long builds"   # run the agent
    python -m src.agent --eval-labels                       # score suggest_labels

suggest_labels is deliberately kNN over the same embeddings rather than asking
the model to pick from a list: it is then *measurable* against the 627 real
labels in the corpus, which keeps this stage consistent with the rest of the
project. --eval-labels needs no API key.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime

import numpy as np

from src import db

MODEL = "claude-opus-5"
RETRIEVAL_MODEL_KEY = "bge-small-ft-hardneg"

_state: dict = {}

# Workflow labels, not content labels. Suggesting "*duplicate" or "info-needed"
# for a new issue is circular -- they describe what triage DID, not what the
# issue is about. vscode prefixes most process labels with "*"; the rest are
# listed explicitly.
PROCESS_LABELS = {
    "info-needed", "triage-needed", "verified", "verification-needed",
    "verification-found", "verification-steps-needed", "insiders-released",
    "unreleased", "new release", "candidate", "confirmed", "spam",
    "author-verification-requested", "z-author-verified", "ai-translated",
}


def _is_content_label(label: str) -> bool:
    return not label.startswith("*") and label not in PROCESS_LABELS


def _retriever(encoder=None):
    """Load the index once; it is 124MB and every tool call shares it.

    encoder is injectable so the label eval can use precomputed vectors and skip
    loading torch entirely -- the same split the retrieval eval uses.
    """
    if "retriever" not in _state:
        from src.retrieve import ModelEncoder, VectorRetriever
        conn = _state.get("conn") or db.connect()
        _state["conn"] = conn
        _state["encoder"] = encoder or ModelEncoder(
            "Musab6969/bge-small-vscode-dup-hardneg")
        _state["retriever"] = VectorRetriever(conn, _state["encoder"], RETRIEVAL_MODEL_KEY)
    return _state["retriever"]


def _meta(numbers: list[int]) -> dict[int, dict]:
    if not numbers:
        return {}
    conn = _state["conn"]
    ph = ",".join(["%s"] * len(numbers))
    with db.cursor(conn) as cur:
        cur.execute(
            f"SELECT i.number, i.title, i.state, i.state_reason, i.url, i.created_at,"
            f" GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',')"
            f" FROM issues i LEFT JOIN issue_labels l ON l.issue_number=i.number"
            f" WHERE i.number IN ({ph}) GROUP BY i.number", numbers)
        return {r[0]: {"number": r[0], "title": r[1], "state": r[2],
                       "state_reason": r[3], "url": r[4],
                       "created_at": str(r[5])[:10],
                       "labels": (r[6] or "").split(",") if r[6] else []}
                for r in cur.fetchall()}


# ---------------------------------------------------------------- tool bodies

def _search_duplicates(text: str, n: int = 5) -> list[dict]:
    r = _retriever()
    # No before_date here: a live triager is searching everything that exists.
    # The eval uses the time filter; this is the deployment case.
    hits = r(text, datetime.now(), n)
    meta = _meta(hits)
    return [meta[h] for h in hits if h in meta]


def _fetch_issue(number: int) -> dict:
    conn = _state.get("conn") or db.connect()
    _state.setdefault("conn", conn)
    with db.cursor(conn) as cur:
        cur.execute("SELECT number,title,body,state,state_reason,url,created_at,"
                    "comment_count FROM issues WHERE number=%s", (number,))
        row = cur.fetchone()
    if not row:
        return {"error": f"issue #{number} not in the corpus"}
    return {"number": row[0], "title": row[1], "body": (row[2] or "")[:2000],
            "state": row[3], "state_reason": row[4], "url": row[5],
            "created_at": str(row[6])[:10], "comment_count": row[7]}


def suggest_labels_knn(text: str, n: int = 5, k: int = 25,
                       before: datetime | None = None) -> list[dict]:
    """Predict labels from the labels of the k nearest issues.

    Each neighbour votes for its labels, weighted by cosine similarity, so a
    close match counts more than a distant one. Purely retrieval-based, which is
    what makes it scoreable against ground truth.
    """
    r = _retriever()
    q = r.encoder(text)
    if q is None:
        return []
    cutoff = (int(np.searchsorted(r.dates, np.datetime64(before), side="left"))
              if before else len(r.numbers))
    if cutoff == 0:
        return []
    sims = r.matrix[:cutoff] @ q
    kk = min(k, cutoff)
    top = np.argpartition(-sims, kk - 1)[:kk]
    top = top[np.argsort(-sims[top])]
    neighbours = [int(r.numbers[i]) for i in top]
    weights = {int(r.numbers[i]): float(sims[i]) for i in top}

    meta = _meta(neighbours)
    votes: dict[str, float] = {}
    for num in neighbours:
        for lab in meta.get(num, {}).get("labels", []):
            if _is_content_label(lab):
                votes[lab] = votes.get(lab, 0.0) + max(0.0, weights[num])
    total = sum(votes.values()) or 1.0
    ranked = sorted(votes.items(), key=lambda x: -x[1])[:n]
    return [{"label": l, "confidence": round(v / total, 3)} for l, v in ranked]


# ---------------------------------------------------------------- eval

def eval_labels(n_pred: int = 3, k: int = 25, limit: int | None = None) -> dict:
    """Score suggest_labels against the labels maintainers actually applied.

    Time-filtered like the retrieval eval: only issues predating the query vote,
    so the score is not inflated by labels applied after the fact.
    """
    from src.retrieve import PrecomputedEncoder
    from src.testset import load
    pairs = load("test")
    if limit:
        pairs = pairs[:limit]
    conn = db.connect()
    _state["conn"] = conn
    enc = PrecomputedEncoder.for_issues(conn, [d for d, _ in pairs], RETRIEVAL_MODEL_KEY)
    r = _retriever(enc)

    nums = [d for d, _ in pairs]
    ph = ",".join(["%s"] * len(nums))
    with db.cursor(conn) as cur:
        cur.execute(f"SELECT number,title,body,created_at FROM issues WHERE number IN ({ph})", nums)
        issues = {r_[0]: (r_[1], r_[2], r_[3]) for r_ in cur.fetchall()}
        cur.execute(f"SELECT issue_number,label FROM issue_labels WHERE issue_number IN ({ph})", nums)
        truth: dict[int, set] = {}
        for num, lab in cur.fetchall():
            truth.setdefault(num, set()).add(lab)

    hits = preds = golds = scored = exact = 0
    for num in nums:
        if num not in issues or not truth.get(num):
            continue
        title, body, created = issues[num]
        got = {p["label"] for p in suggest_labels_knn(
            db.issue_text(title, body), n_pred, k, before=created)}
        gold = {g for g in truth[num] if _is_content_label(g)}
        if not gold:
            continue
        scored += 1
        hits += len(got & gold)
        preds += len(got)
        golds += len(gold)
        if got & gold:
            exact += 1
    p = hits / preds if preds else 0.0
    rc = hits / golds if golds else 0.0
    return {"issues_scored": scored, "precision": round(p, 4), "recall": round(rc, 4),
            "f1": round(2 * p * rc / (p + rc), 4) if p + rc else 0.0,
            "any_correct": round(exact / scored, 4) if scored else 0.0,
            "labels_predicted": n_pred, "neighbours": k}


# ---------------------------------------------------------------- agent

def run_agent(prompt: str) -> None:
    import anthropic
    from anthropic import beta_tool

    @beta_tool
    def search_duplicates(text: str, n: int = 5) -> list[dict]:
        """Find existing vscode issues that may already cover this text.

        Args:
            text: The new issue's title and description.
            n: How many candidates to return (1-20).
        """
        return _search_duplicates(text, min(max(n, 1), 20))

    @beta_tool
    def fetch_issue(number: int) -> dict:
        """Fetch one issue's full details by number.

        Args:
            number: The GitHub issue number.
        """
        return _fetch_issue(number)

    @beta_tool
    def suggest_labels(text: str, n: int = 5) -> list[dict]:
        """Suggest labels for an issue, from the labels of similar issues.

        Args:
            text: The issue's title and description.
            n: How many labels to suggest (1-10).
        """
        return suggest_labels_knn(text, min(max(n, 1), 10))

    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=(
            "You triage GitHub issues for microsoft/vscode. Given a new issue: "
            "search for possible duplicates, fetch any that look close to confirm, "
            "and suggest labels. Be decisive and brief. State clearly whether you "
            "think it IS a duplicate and of what, or that it looks new. "
            "Similarity is not duplication -- a bug report and a feature request "
            "about the same area are not duplicates."
        ),
        tools=[search_duplicates, fetch_issue, suggest_labels],
        messages=[{"role": "user", "content": prompt}],
    )
    for message in runner:
        for block in message.content:
            if block.type == "text" and block.text.strip():
                print(block.text)
            elif block.type == "tool_use":
                print(f"  [tool] {block.name}({block.input})")


def main() -> None:
    ap = argparse.ArgumentParser(description="vscode issue triage agent")
    ap.add_argument("prompt", nargs="?", help="issue text to triage")
    ap.add_argument("--eval-labels", action="store_true", help="score suggest_labels")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--labels", type=int, default=3)
    ap.add_argument("--neighbours", type=int, default=25)
    args = ap.parse_args()

    if args.eval_labels:
        import json
        print(json.dumps(eval_labels(args.labels, args.neighbours, args.limit), indent=1))
        return
    if not args.prompt:
        ap.error("give issue text, or --eval-labels")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set (the agent needs it; "
                         "--eval-labels does not)")
    run_agent(args.prompt)


if __name__ == "__main__":
    main()
