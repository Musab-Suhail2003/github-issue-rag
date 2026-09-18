"""Stage 0 corpus probe -- THROWAWAY. Delete after the numbers land in NOTES.md.

One question: how many usable (duplicate -> canonical) pairs actually survive?
That count is multiplicative, and every factor needs measuring:

    union(label:*duplicate, reason:duplicate)
      x  canonical number extractable from comment prose
      x  canonical is an Issue, not a PR
      x  canonical created inside the corpus date bound

Recon established that microsoft/vscode does NOT populate the structured
MarkedAsDuplicateEvent.canonical field -- it was null on all 25 issues sampled.
The target only exists as prose in a bot comment, so extraction is regex over
comment bodies and the hit rate is an empirical unknown, not an assumption.
"""

from __future__ import annotations

import collections
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_OWNER, REPO_NAME = "microsoft", "vscode"
CORPUS_START = "2024-01-01"  # the scoping bound from CLAUDE.md, under test here
API = "https://api.github.com/graphql"
OUT = Path(__file__).resolve().parent.parent / "probe_out"

# Sample budget. Stratified by year because the triage bot's comment wording has
# changed over three years -- sampling only recent issues would overstate the
# extraction rate on the older half of the corpus.
YEARS = [("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-12-31"),
         ("2026-01-01", "2026-12-31")]
PER_PAGE = 25          # keep GraphQL node count per request modest
PAGES_PER_QUERY = 2    # -> up to 50 issues per (year, signal, sort) cell

# Ordered by trust: first pattern to match wins, and we record WHICH one fired
# so stage 2 can decide how much of this battery is worth keeping.
# A duplicate target appears in three surface forms -- bare #123, a markdown
# link [#123](url), or a full issues URL. Normalising them into one alternation
# is worth it: the markdown-link form alone accounted for 15 missed pairs when
# the bracket was not optional.
REF = r"\[?(?:#|https://github\.com/microsoft/vscode/issues/)(\d+)"

# Ordered by trust: first pattern to match wins, and we record WHICH one fired
# so stage 2 can decide how much of this battery is worth keeping.
PATTERNS = [
    ("dup_of",     re.compile(r"\b(?:duplicate|dup)\s+of\s+" + REF, re.I)),
    ("dup_colon",  re.compile(r"^\s*/?(?:duplicate|dup)\b[:\s]+" + REF, re.I | re.M)),
    ("same_as",    re.compile(r"covering the same as\s+" + REF, re.I)),
    # Weakest of the strong set -- "tracked in #123" is a maintainer pointing at
    # an existing issue, which is usually but not always a true duplicate.
    ("tracked_in", re.compile(r"\b(?:tracked|fixed|covered)\s+(?:in|by|here)\b[^.\n]{0,40}?" + REF, re.I)),
]
# Deliberately separate: a bare #1234 anywhere in a thread is far too noisy to
# trust as ground truth. Measured only to show how much we would gain by
# accepting the noise -- and therefore why we don't.
WEAK = re.compile(r"(?:https://github\.com/microsoft/vscode/issues/|#)(\d+)")

BOT_LOGINS = {"vs-code-engineering", "vscodebot", "VSCodeTriageBot", "github-actions"}

# vscode issue text is full of stack traces, version strings and file paths.
# Quantifying that here is the evidence for why stage 4 (BM25) should beat
# dense-only retrieval -- embeddings smear these tokens, lexical search doesn't.
NOISE_SIGNALS = [
    ("stack_frame",  re.compile(r"^\s*at\s+\S+\s*\(.*:\d+:\d+\)", re.M)),
    ("version_str",  re.compile(r"\b(?:Version|VS Code version)\s*:\s*\d+\.\d+", re.I)),
    ("file_path",    re.compile(r"(?:[a-zA-Z]:\\|/)(?:[\w.-]+[/\\]){2,}[\w.-]+")),
    ("code_fence",   re.compile(r"```")),
    ("extension_id", re.compile(r"\b[\w-]+\.[\w-]+@\d+\.\d+\.\d+\b")),
]


def gql(session: requests.Session, query: str, tries: int = 4) -> dict:
    """POST a GraphQL query, retrying on secondary-rate-limit/5xx with backoff."""
    for attempt in range(tries):
        r = session.post(API, json={"query": query}, timeout=90)
        if r.status_code in (502, 503, 504) or (r.status_code == 403 and "rate limit" in r.text.lower()):
            time.sleep(2 ** attempt * 5)
            continue
        r.raise_for_status()
        payload = r.json()
        if "errors" in payload:
            # A partial response with data is usable; a hard error is not.
            if payload.get("data") is None:
                raise RuntimeError(json.dumps(payload["errors"])[:600])
            print(f"  ! partial errors: {json.dumps(payload['errors'])[:200]}", file=sys.stderr)
        return payload["data"]
    raise RuntimeError("giving up after retries")


def count(session, qualifiers: str) -> int:
    q = f'''query {{
      search(query: "repo:{REPO_OWNER}/{REPO_NAME} is:issue {qualifiers}", type: ISSUE, first: 1) {{ issueCount }}
    }}'''
    return gql(session, q)["search"]["issueCount"]


def sample_page(session, qualifiers: str, sort: str, cursor: str | None):
    after = f', after: "{cursor}"' if cursor else ""
    q = f'''query {{
      search(query: "repo:{REPO_OWNER}/{REPO_NAME} is:issue {qualifiers} sort:{sort}",
             type: ISSUE, first: {PER_PAGE}{after}) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ ... on Issue {{
          number createdAt title body stateReason
          labels(first: 20) {{ nodes {{ name }} }}
          comments(last: 20) {{ nodes {{ author {{ login }} body }} }}
        }} }}
      }}
      rateLimit {{ cost remaining }}
    }}'''
    d = gql(session, q)
    return d["search"], d["rateLimit"]


def extract_canonical(issue: dict):
    """Return (canonical_number, pattern_name, author_login) or (None, None, None).

    Scans comments newest-first: the duplicate declaration is almost always the
    closing comment, and later statements supersede earlier speculation.
    """
    comments = list(reversed(issue.get("comments", {}).get("nodes", []) or []))
    for name, rx in [(n, r) for n, r in PATTERNS]:
        for c in comments:
            m = rx.search(c.get("body") or "")
            if m:
                num = int(m.group(1))
                if num != issue["number"]:  # guard against self-reference
                    return num, name, ((c.get("author") or {}).get("login"))
    for c in comments:  # weak fallback, measured but not trusted
        m = WEAK.search(c.get("body") or "")
        if m and int(m.group(1)) != issue["number"]:
            return int(m.group(1)), "weak_bare_ref", ((c.get("author") or {}).get("login"))
    return None, None, None


def resolve(session, numbers: list[int]) -> dict[int, dict]:
    """Batch-resolve issue numbers via aliases.

    Uses issueOrPullRequest, not issue(): repository.issue(number:) returns null
    for a PR number, which would silently look identical to 'deleted issue'.
    We need PRs distinguishable so they can be dropped, per CLAUDE.md.
    """
    out: dict[int, dict] = {}
    CHUNK = 50
    for i in range(0, len(numbers), CHUNK):
        chunk = numbers[i:i + CHUNK]
        aliases = "\n".join(
            f'n{n}: issueOrPullRequest(number: {n}) {{ __typename '
            f'... on Issue {{ number createdAt stateReason }} '
            f'... on PullRequest {{ number createdAt }} }}' for n in chunk)
        q = f'query {{ repository(owner: "{REPO_OWNER}", name: "{REPO_NAME}") {{\n{aliases}\n}} }}'
        data = gql(session, q)["repository"] or {}
        for n in chunk:
            out[n] = data.get(f"n{n}")  # None => deleted / transferred / never existed
        print(f"  resolved {min(i + CHUNK, len(numbers))}/{len(numbers)}", file=sys.stderr)
    return out


def main() -> None:
    load_dotenv()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN missing from .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"bearer {token}",
                            "User-Agent": "issue-rag-probe"})
    OUT.mkdir(exist_ok=True)

    # ---------- 1. denominators ----------
    print("== corpus counts ==")
    totals = {
        "all":        count(session, f"created:>={CORPUS_START}"),
        "open":       count(session, f"created:>={CORPUS_START} is:open"),
        "label":      count(session, f"created:>={CORPUS_START} label:*duplicate"),
        "reason":     count(session, f"created:>={CORPUS_START} reason:duplicate"),
        "both":       count(session, f"created:>={CORPUS_START} label:*duplicate reason:duplicate"),
    }
    # Search ANDs qualifiers, so union must come from inclusion-exclusion.
    totals["union"] = totals["label"] + totals["reason"] - totals["both"]
    for k, v in totals.items():
        print(f"  {k:8} {v:>7,}")

    per_year = {}
    for lo, hi in YEARS:
        y = lo[:4]
        lab = count(session, f"created:{lo}..{hi} label:*duplicate")
        rea = count(session, f"created:{lo}..{hi} reason:duplicate")
        bot = count(session, f"created:{lo}..{hi} label:*duplicate reason:duplicate")
        per_year[y] = {"label": lab, "reason": rea, "both": bot, "union": lab + rea - bot,
                       "all": count(session, f"created:{lo}..{hi}")}
        print(f"  {y}: all={per_year[y]['all']:>6,}  union_dup={per_year[y]['union']:>5,}")

    # ---------- 2. stratified sample ----------
    print("\n== sampling ==", file=sys.stderr)
    seen: dict[int, dict] = {}
    for lo, hi in YEARS:
        for signal in ("label:*duplicate", "reason:duplicate"):
            for sort in ("created-desc", "created-asc"):  # both ends of each year
                cursor, got = None, 0
                for _ in range(PAGES_PER_QUERY):
                    res, rl = sample_page(session, f"created:{lo}..{hi} {signal}", sort, cursor)
                    for node in res["nodes"]:
                        if node and node.get("number") and node["number"] not in seen:
                            node["_year"] = lo[:4]
                            seen[node["number"]] = node
                    got += len(res["nodes"])
                    if not res["pageInfo"]["hasNextPage"]:
                        break
                    cursor = res["pageInfo"]["endCursor"]
                print(f"  {lo[:4]} {signal:20} {sort:12} +{got:3}  "
                      f"(unique so far {len(seen)}, rl_remaining {rl['remaining']})", file=sys.stderr)

    sample = list(seen.values())
    print(f"\n== sample: {len(sample)} unique duplicate-marked issues ==")

    # ---------- 3. extraction ----------
    pattern_hits = collections.Counter()
    author_hits = collections.Counter()
    extracted: dict[int, int] = {}
    for iss in sample:
        num, pat, author = extract_canonical(iss)
        if num is None:
            pattern_hits["NO_MATCH"] += 1
            continue
        pattern_hits[pat] += 1
        author_hits["bot" if author in BOT_LOGINS else f"other:{author}"] += 1
        iss["_canonical"], iss["_pattern"] = num, pat
        extracted[iss["number"]] = num

    print("\n-- which pattern fired --")
    for pat, n in pattern_hits.most_common():
        print(f"  {pat:16} {n:>4}  ({n / len(sample):.1%})")
    strong = sum(v for k, v in pattern_hits.items() if k not in ("NO_MATCH", "weak_bare_ref"))
    print(f"  {'STRONG TOTAL':16} {strong:>4}  ({strong / len(sample):.1%})")

    # ---------- 4. resolve canonicals ----------
    targets = sorted({v for k, v in extracted.items()
                      if sample_pattern(seen, k) != "weak_bare_ref"})
    print(f"\n== resolving {len(targets)} distinct canonical targets ==")
    resolved = resolve(session, targets)

    kinds = collections.Counter()
    in_corpus = 0
    usable_pairs = []
    for dup_num, can_num in extracted.items():
        if sample_pattern(seen, dup_num) == "weak_bare_ref":
            continue
        info = resolved.get(can_num)
        if info is None:
            kinds["missing/deleted"] += 1
            continue
        kinds[info["__typename"]] += 1
        if info["__typename"] != "Issue":
            continue
        if info["createdAt"] >= CORPUS_START:
            in_corpus += 1
            usable_pairs.append((dup_num, can_num))

    print("\n-- canonical target type --")
    for k, v in kinds.most_common():
        print(f"  {k:16} {v:>4}")

    # ---------- 5. the funnel ----------
    n = len(sample)
    r_extract = strong / n
    issue_targets = kinds.get("Issue", 0)
    r_is_issue = issue_targets / strong if strong else 0
    r_in_corpus = in_corpus / issue_targets if issue_targets else 0
    est = totals["union"] * r_extract * r_is_issue * r_in_corpus

    print(f"""
================= FUNNEL =================
  duplicate-marked in corpus window   {totals['union']:>7,}
  x canonical extractable (strong)    {r_extract:>7.1%}
  x canonical is an Issue not a PR    {r_is_issue:>7.1%}
  x canonical created >= {CORPUS_START}  {r_in_corpus:>7.1%}
  ------------------------------------------
  ESTIMATED USABLE PAIRS              {est:>7,.0f}
  (measured directly in sample: {len(usable_pairs)} of {n})
==========================================""")

    # per-year extraction rate -- exposes drift in bot wording over time
    print("\n-- extraction rate by year (sampling bias check) --")
    by_year = collections.defaultdict(lambda: [0, 0])
    for iss in sample:
        by_year[iss["_year"]][1] += 1
        if iss.get("_pattern") and iss["_pattern"] != "weak_bare_ref":
            by_year[iss["_year"]][0] += 1
    for y in sorted(by_year):
        hit, tot = by_year[y]
        print(f"  {y}: {hit:>3}/{tot:<3} = {hit / tot:.1%}")

    # ---------- 6. text character (motivates stage 4) ----------
    lens = [len(i.get("body") or "") for i in sample]
    print(f"\n-- body text --")
    print(f"  median length {statistics.median(lens):,.0f} chars, "
          f"p90 {statistics.quantiles(lens, n=10)[-1]:,.0f}, empty {sum(1 for l in lens if l < 10)}")
    for name, rx in NOISE_SIGNALS:
        hits = sum(1 for i in sample if rx.search(i.get("body") or ""))
        print(f"  {name:14} {hits:>4}/{n} = {hits / n:.1%}")

    (OUT / "sample.json").write_text(json.dumps(sample, indent=1))
    (OUT / "summary.json").write_text(json.dumps({
        "totals": totals, "per_year": per_year, "sample_size": n,
        "pattern_hits": dict(pattern_hits), "author_hits": dict(author_hits),
        "target_kinds": dict(kinds), "rates": {
            "extract": r_extract, "is_issue": r_is_issue, "in_corpus": r_in_corpus},
        "estimated_usable_pairs": est,
        "usable_pairs_in_sample": usable_pairs,
    }, indent=1))
    print(f"\nwrote {OUT}/sample.json and summary.json")


def sample_pattern(seen: dict, num: int) -> str | None:
    return seen.get(num, {}).get("_pattern")


if __name__ == "__main__":
    main()
