"""MariaDB connection and idempotent upserts. Batch side only -- the Space
never imports this.

Uses PyMySQL because mysql-connector corrupts large multi-row upserts. See
NOTES.md, stage 1.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

import pymysql
import pymysql.cursors
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"


def connect(autocommit: bool = False):
    """Open a connection using credentials from .env."""
    load_dotenv(ROOT / ".env")
    missing = [k for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
               if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"missing from .env: {', '.join(missing)}")
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        charset="utf8mb4",
        autocommit=autocommit,
    )


@contextmanager
def cursor(conn, dictionary: bool = False):
    cur = conn.cursor(pymysql.cursors.DictCursor) if dictionary else conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def init_schema(conn) -> None:
    """Apply schema.sql. Safe to re-run.

    Splits on ";" -- fine because the schema has no stored routines.
    """
    sql = SCHEMA_PATH.read_text()
    sql = re.sub(r"^\s*--.*$", "", sql, flags=re.M)  # strip comment lines
    with cursor(conn) as cur:
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                cur.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------- upserts

# ON DUPLICATE KEY UPDATE, not INSERT IGNORE: re-running should refresh a row,
# since state, labels and comment counts change over time. That is what makes an
# interrupted crawl safe to restart.

_ISSUE_COLS = ("number", "title", "body", "state", "state_reason", "author", "url",
               "created_at", "updated_at", "closed_at", "comment_count",
               "comments_fetched")
_ISSUE_UPDATE = ("title", "body", "state", "state_reason", "author", "url",
                 "updated_at", "closed_at", "comment_count", "comments_fetched")

_COMMENT_COLS = ("id", "issue_number", "author", "body", "created_at")
_COMMENT_UPDATE = ("author", "body")


def _bulk(conn, table: str, cols: Sequence[str], rows: Sequence[tuple],
          update: Sequence[str] = (), ignore: bool = False,
          chunk: int = 100) -> int:
    """One INSERT per chunk of rows.

    chunk stays small because bodies are MEDIUMTEXT and a big batch can approach
    max_allowed_packet.
    """
    if not rows:
        return 0
    placeholder = "(" + ",".join(["%s"] * len(cols)) + ")"
    verb = "INSERT IGNORE INTO" if ignore else "INSERT INTO"
    tail = ""
    if update:
        tail = " ON DUPLICATE KEY UPDATE " + ", ".join(f"{c}=VALUES({c})" for c in update)
    written = 0
    with cursor(conn) as cur:
        for i in range(0, len(rows), chunk):
            block = rows[i:i + chunk]
            sql = (f"{verb} {table} ({','.join(cols)}) VALUES "
                   + ",".join([placeholder] * len(block)) + tail)
            cur.execute(sql, [value for row in block for value in row])
            written += len(block)
    return written


def upsert_issues(conn, rows: Sequence[tuple]) -> int:
    return _bulk(conn, "issues", _ISSUE_COLS, rows, _ISSUE_UPDATE)


def upsert_comments(conn, rows: Sequence[tuple]) -> int:
    return _bulk(conn, "comments", _COMMENT_COLS, rows, _COMMENT_UPDATE)


def replace_labels(conn, issue_numbers: Iterable[int], rows: Sequence[tuple]) -> int:
    """Replace an issue's labels.

    Deletes first because labels get removed during triage; inserting alone
    would leave stale rows.
    """
    nums = list(issue_numbers)
    if nums:
        with cursor(conn) as cur:
            placeholders = ",".join(["%s"] * len(nums))
            cur.execute(
                f"DELETE FROM issue_labels WHERE issue_number IN ({placeholders})",
                nums,
            )
    return _bulk(conn, "issue_labels", ("issue_number", "label"), rows, ignore=True)


# ---------------------------------------------------------------- text

def issue_text(title: str | None, body: str | None, max_chars: int = 8000) -> str:
    """Turn an issue into the one text string everything else uses.

    Shared by eval.py and embed.py so the query side and document side can never
    compose text differently.
    """
    parts = [(title or "").strip()]
    if body:
        parts.append(body.strip())
    return "\n\n".join(p for p in parts if p)[:max_chars]


# ---------------------------------------------------------------- state

def get_state(conn, key: str) -> str | None:
    with cursor(conn) as cur:
        cur.execute("SELECT v FROM fetch_state WHERE k=%s", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def set_state(conn, key: str, value: str | None) -> None:
    with cursor(conn) as cur:
        cur.execute(
            "INSERT INTO fetch_state (k, v) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE v=VALUES(v)",
            (key, value),
        )


def counts(conn) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with cursor(conn) as cur:
        for table in ("issues", "comments", "issue_labels"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = cur.fetchone()[0]
        cur.execute("SELECT MIN(created_at), MAX(created_at) FROM issues")
        lo, hi = cur.fetchone()
        out["created_range"] = (str(lo), str(hi))
        cur.execute(
            "SELECT state_reason, COUNT(*) FROM issues "
            "WHERE state_reason IS NOT NULL GROUP BY state_reason"
        )
        out["state_reason"] = dict(cur.fetchall())
    return out


if __name__ == "__main__":
    import json
    import sys

    conn = connect()
    if "--init" in sys.argv:
        init_schema(conn)
        print(f"applied {SCHEMA_PATH}")
    print(json.dumps(counts(conn), indent=1, default=str))
    conn.close()
