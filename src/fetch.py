"""GitHub GraphQL ingestion -> MariaDB.

Three modes:

    python -m src.fetch                  # backfill the corpus window (resumable)
    python -m src.fetch --since auto     # incremental refresh from fetch_state
    python -m src.fetch --numbers 1,2,3  # top up specific issues

Why the `issues` connection rather than `search`: search caps at 1,000 results
per query, so covering 85k issues through it would mean slicing the date range
into windows and hoping none exceeds the cap. The connection cursors the whole
set. It also excludes pull requests by construction -- `issues` and
`pullRequests` are separate connections -- which is the guarantee CLAUDE.md asks
for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from src import db

REPO_OWNER, REPO_NAME = "microsoft", "vscode"
CORPUS_START = datetime(2024, 1, 1)
API = "https://api.github.com/graphql"
CORPUS_EST = 84_877  # measured in stage 0, used only for progress ETA

ISSUE_FIELDS = """
fragment IssueFields on Issue {
  number title body state stateReason url createdAt updatedAt closedAt
  author { login }
  labels(first: 30) { nodes { name } }
  comments(last: 20) {
    totalCount
    nodes { databaseId author { login } body createdAt }
  }
}
"""

# comments(last: 20) is deliberate. The duplicate declaration that stage 2 needs
# is essentially always the closing comment, and `last` puts the end of the
# thread in reach without paginating every thread. comment_count vs
# comments_fetched records exactly where that truncation bites.

Q_BACKFILL = ISSUE_FIELDS + """
query Backfill($cursor: String, $pageSize: Int!) {
  repository(owner: "%s", name: "%s") {
    issues(first: $pageSize, orderBy: {field: CREATED_AT, direction: DESC}, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { ...IssueFields }
    }
  }
  rateLimit { cost remaining resetAt }
}
""" % (REPO_OWNER, REPO_NAME)

Q_SINCE = ISSUE_FIELDS + """
query Since($cursor: String, $pageSize: Int!, $since: DateTime!) {
  repository(owner: "%s", name: "%s") {
    issues(first: $pageSize, orderBy: {field: UPDATED_AT, direction: DESC},
           filterBy: {since: $since}, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { ...IssueFields }
    }
  }
  rateLimit { cost remaining resetAt }
}
""" % (REPO_OWNER, REPO_NAME)


def gql(session: requests.Session, query: str, variables: dict, tries: int = 5) -> dict:
    for attempt in range(tries):
        try:
            r = session.post(API, json={"query": query, "variables": variables}, timeout=120)
        except requests.RequestException as exc:
            print(f"  ! network {exc.__class__.__name__}, retrying", file=sys.stderr)
            time.sleep(2 ** attempt * 5)
            continue
        if r.status_code in (502, 503, 504) or (
            r.status_code == 403 and "rate limit" in r.text.lower()
        ):
            wait = 2 ** attempt * 10
            print(f"  ! HTTP {r.status_code}, sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        payload = r.json()
        if "errors" in payload and payload.get("data") is None:
            raise RuntimeError(json.dumps(payload["errors"])[:600])
        if "errors" in payload:
            print(f"  ! partial: {json.dumps(payload['errors'])[:200]}", file=sys.stderr)
        return payload["data"]
    raise RuntimeError("giving up after retries")


def respect_rate_limit(rl: dict) -> None:
    """GraphQL points regenerate hourly; sleeping beats a 403 mid-crawl."""
    if rl and rl.get("remaining", 9999) < 150:
        reset = datetime.fromisoformat(rl["resetAt"].replace("Z", "+00:00"))
        wait = max(0, (reset - datetime.now(timezone.utc)).total_seconds()) + 5
        print(f"  ! rate limit low ({rl['remaining']}), sleeping {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)


def ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # Store naive UTC: MariaDB DATETIME is timezone-less and every timestamp
    # from the API is already UTC, so carrying tzinfo would only invite
    # comparison bugs against CORPUS_START.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def clean(text: str | None) -> str | None:
    # NUL bytes appear occasionally in pasted terminal output and upset both the
    # connector and downstream tokenisers.
    return text.replace("\x00", "") if text else text


def rows_from(node: dict):
    issue = (
        node["number"], clean(node["title"]), clean(node["body"]),
        node["state"], node.get("stateReason"),
        (node.get("author") or {}).get("login"), node["url"],
        ts(node["createdAt"]), ts(node["updatedAt"]), ts(node.get("closedAt")),
        node["comments"]["totalCount"], len(node["comments"]["nodes"]),
    )
    comments = [
        (c["databaseId"], node["number"], (c.get("author") or {}).get("login"),
         clean(c.get("body")), ts(c["createdAt"]))
        for c in node["comments"]["nodes"] if c.get("databaseId")
    ]
    labels = [(node["number"], lab["name"]) for lab in node["labels"]["nodes"]]
    return issue, comments, labels


def write_batch(conn, nodes: list[dict]) -> None:
    issues, comments, labels = [], [], []
    for node in nodes:
        i, c, l = rows_from(node)
        issues.append(i)
        comments.extend(c)
        labels.extend(l)
    db.upsert_issues(conn, issues)
    # Labels and comments carry a FK to issues, so issues must land first.
    db.replace_labels(conn, [i[0] for i in issues], labels)
    db.upsert_comments(conn, comments)


def crawl(conn, session, mode: str, since: datetime | None,
          page_size: int, limit: int | None) -> int:
    query = Q_BACKFILL if mode == "backfill" else Q_SINCE
    cursor_key = f"{mode}_cursor"
    cursor = db.get_state(conn, cursor_key)
    if cursor:
        print(f"resuming {mode} from saved cursor")

    # Watermark is taken BEFORE the crawl starts, not after. An issue updated
    # while the crawl is running would otherwise fall in the gap between the
    # page we already passed and a watermark set at the end.
    started = datetime.now(timezone.utc)

    total = skipped = page = 0
    t0 = time.time()
    try:
        while True:
            variables = {"cursor": cursor, "pageSize": page_size}
            if mode == "since":
                variables["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            data = gql(session, query, variables)
            conn_block = data["repository"]["issues"]
            nodes = [n for n in conn_block["nodes"] if n]

            keep, stop = [], False
            for node in nodes:
                created, updated = ts(node["createdAt"]), ts(node["updatedAt"])
                if mode == "backfill" and created < CORPUS_START:
                    stop = True  # ordered CREATED_AT DESC, so everything after is older
                    break
                if mode == "since" and updated < since:
                    stop = True  # ordered UPDATED_AT DESC
                    break
                if created < CORPUS_START:
                    skipped += 1  # touched recently but outside the corpus window
                    continue
                keep.append(node)

            if keep:
                write_batch(conn, keep)
                total += len(keep)

            page += 1
            cursor = conn_block["pageInfo"]["endCursor"]
            db.set_state(conn, cursor_key, None if stop else cursor)
            conn.commit()  # checkpoint: a kill here costs at most one page

            rl = data.get("rateLimit") or {}
            rate = total / max(1e-6, time.time() - t0)
            oldest = ts(keep[-1]["createdAt"]) if keep else None
            eta = (CORPUS_EST - total) / rate / 60 if mode == "backfill" and rate else 0
            print(f"  page {page:>4}  +{len(keep):>3}  total {total:>6,}  "
                  f"{rate:>5.1f}/s  oldest {str(oldest)[:10]}  "
                  f"rl {rl.get('remaining', '?')}"
                  + (f"  eta {eta:.0f}m" if eta else ""))

            if stop or not conn_block["pageInfo"]["hasNextPage"]:
                break
            if limit and total >= limit:
                print(f"  stopping at --limit {limit}")
                return total
            respect_rate_limit(rl)
    except KeyboardInterrupt:
        conn.commit()
        print(f"\ninterrupted -- {total:,} issues written, cursor saved. "
              f"Re-run to resume.", file=sys.stderr)
        raise SystemExit(130)

    db.set_state(conn, cursor_key, None)
    if mode == "backfill":
        db.set_state(conn, "backfill_complete", "1")
    db.set_state(conn, "last_fetch_completed_at", started.strftime("%Y-%m-%dT%H:%M:%SZ"))
    conn.commit()
    if skipped:
        print(f"  ({skipped:,} touched but outside the corpus window)")
    return total


def fetch_numbers(conn, session, numbers: list[int], chunk: int = 40) -> int:
    """Fetch specific issue numbers regardless of the date bound.

    This is the escape hatch for the open question in NOTES.md: 22.7% of
    duplicate pairs point at a canonical created before 2024-01-01. If those are
    later admitted to the corpus, this is a short top-up run rather than a
    refetch of everything.
    """
    written = 0
    for i in range(0, len(numbers), chunk):
        batch = numbers[i:i + chunk]
        aliases = "\n".join(f"n{n}: issue(number: {n}) {{ ...IssueFields }}" for n in batch)
        query = ISSUE_FIELDS + (
            f'query {{ repository(owner: "{REPO_OWNER}", name: "{REPO_NAME}") '
            f"{{\n{aliases}\n}} rateLimit {{ remaining resetAt }} }}"
        )
        data = gql(session, query, {})
        repo = data["repository"] or {}
        nodes = [repo.get(f"n{n}") for n in batch]
        nodes = [n for n in nodes if n]  # null => the number is a PR or was deleted
        if nodes:
            write_batch(conn, nodes)
            conn.commit()
            written += len(nodes)
        print(f"  {min(i + chunk, len(numbers))}/{len(numbers)} requested, {written} written")
        respect_rate_limit(data.get("rateLimit") or {})
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest microsoft/vscode issues into MariaDB")
    ap.add_argument("--since", help="ISO date, or 'auto' to read fetch_state")
    ap.add_argument("--numbers", help="comma-separated issue numbers to top up")
    ap.add_argument("--page-size", type=int, default=50)
    ap.add_argument("--limit", type=int, help="stop after N issues (smoke test)")
    args = ap.parse_args()

    load_dotenv()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN missing from .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"bearer {token}",
                            "User-Agent": "issue-rag-fetch"})

    conn = db.connect()
    db.init_schema(conn)  # idempotent, so a fresh clone just works

    if args.numbers:
        nums = [int(x) for x in args.numbers.replace(" ", "").split(",") if x]
        print(f"== topping up {len(nums)} issues by number ==")
        fetch_numbers(conn, session, nums)
    else:
        if args.since:
            raw = db.get_state(conn, "last_fetch_completed_at") if args.since == "auto" else args.since
            if not raw:
                sys.exit("--since auto, but fetch_state has no last_fetch_completed_at yet")
            since = ts(raw) if "T" in raw else datetime.fromisoformat(raw)
            print(f"== incremental refresh, updated since {since} ==")
            crawl(conn, session, "since", since, args.page_size, args.limit)
        else:
            print(f"== backfill, issues created >= {CORPUS_START.date()} ==")
            crawl(conn, session, "backfill", None, args.page_size, args.limit)

    print("\n" + json.dumps(db.counts(conn), indent=1, default=str))
    conn.close()


if __name__ == "__main__":
    main()
