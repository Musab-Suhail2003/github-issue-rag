# NOTES

Running log of what each stage produced. A stage that ran but was not logged
did not happen.

---

## Stage 0 — Corpus probe (2026-09-18)

**Goal:** confirm there are enough maintainer-marked duplicate pairs in
`microsoft/vscode` issues created >= 2024-01-01 to support a measured eval.
No retrieval yet, so no recall/MRR numbers — the deliverable is a go/no-go.

**Config:** `scripts/probe.py`, 545 unique duplicate-marked issues sampled,
stratified across 2024/2025/2026 and across both duplicate signals, drawn from
both ends of each year (`sort:created-asc` and `created-desc`) to limit recency
bias. ~40 GraphQL requests, well inside the 5,000/hr limit.

### Corpus size

| | count |
|---|---|
| issues created >= 2024-01-01 | **84,877** |
| — of which open | 14,591 |
| `label:*duplicate` | 5,370 |
| `reason:duplicate` (GitHub native close reason) | 3,194 |
| both signals | 526 |
| **union (duplicate-marked)** | **8,038** |

Per year: 2024 → 19,219 issues / 1,941 dup · 2025 → 34,005 / 3,464 ·
2026 (to Sept) → 31,653 / 2,633.

> **Correction to CLAUDE.md:** the corpus is 84,877 issues, not the ~40k
> estimated. Vectors at 384 dims float32 ≈ **130MB**, not 60MB. Still fine for
> brute-force numpy, but stage 1's fetch is ~850 paged requests, not ~400.

### The finding that shapes stage 1

**The canonical target is not in any structured GraphQL field.**
`MarkedAsDuplicateEvent.canonical` returned `null` on every issue sampled —
vscode's triage bot sets the close reason but never fires the "mark as
duplicate" UI path that populates it. The target exists only as prose:

> `vs-code-engineering`: "This issue is a duplicate of
> https://github.com/microsoft/vscode/issues/253137."

So the test set must be built by **regex over comment bodies**, which means
`fetch.py` has to persist comments, not just title/body. Schema implication,
decided before the fetch rather than after it.

### Extraction funnel

```
duplicate-marked in corpus window     8,038
x canonical extractable (strong)      40.4%
x canonical is an Issue not a PR     100.0%
x canonical created >= 2024-01-01     77.3%
--------------------------------------------
ESTIMATED USABLE PAIRS                ~2,507     (measured: 170 of 545 sampled)
```

**Verdict: GO.** Target was >=300 pairs for a stable eval; ~2,500 is ample.
Even recall@1 on a held-out slice will be meaningful.

### Which pattern fired (n=545)

| pattern | n | share |
|---|---|---|
| `NO_MATCH` | 190 | 34.9% |
| `dup_of` — "duplicate/dup of #N \| [#N](url) \| url" | 139 | 25.5% |
| `weak_bare_ref` — bare `#N` anywhere (**excluded**) | 135 | 24.8% |
| `dup_colon` — "/duplicate: #N" | 74 | 13.6% |
| `tracked_in` — "tracked in #N" (weakest, kept) | 7 | 1.3% |
| **strong total** | **220** | **40.4%** |

By year: 2024 40.0% · 2025 35.8% · 2026 46.5%. No collapse on older issues, so
the battery is not overfit to current bot wording.

**The 34.9% `NO_MATCH` is irreducible, not a regex gap.** Two causes, both
checked by hand:
- The bot's most common closing comment names no number at all: *"We figured
  it's covering the same as **another one we already have**."*
- 38% of `NO_MATCH` issues have **zero comments** — closed as duplicate silently.

**`weak_bare_ref` is deliberately excluded.** 149 of 150 sit in human comments,
and inspection showed they are roughly half false: `#236527 → #236647` is a real
duplicate, but `#237059 → #142813` was "Essential cause: <url>" (causal, not
duplicate) and `#236485 → #236390` was "there might be a number of related
issues" followed by three links. Admitting these would corrupt ground truth to
buy ~25% more pairs. Not worth it — the whole project rests on this label.

Adding the markdown-link form `[#123](url)` to the reference alternation moved
extraction 37.6% → 40.4% (+15 pairs). Cheap win, kept.

### Text character (evidence for stage 4)

Body length: median 1,773 chars, p90 4,472, 1 empty of 545.

| signal | share of issues |
|---|---|
| file paths | 53.2% |
| fenced code blocks | 47.2% |
| version strings (`Version: 1.x`) | 38.9% |
| stack frames (`at fn (file:12:34)`) | **2.0%** |

> **Correction to CLAUDE.md:** the stated rationale for BM25 is that vscode
> issues are "full of stack traces." Measured, stack traces appear in only
> **2.0%** of issues. The BM25 argument still holds, but the tokens that carry
> it are **file paths, code blocks and version strings**, not stack traces.
> Stage 4 should be justified on those, and the ablation examples chosen
> accordingly.

### Open decision for stage 1

22.7% of extractable pairs point at a canonical created **before** 2024-01-01
and are currently discarded. Recovering them does not require moving the corpus
bound — it requires letting the corpus include any issue that *is* a canonical
target, regardless of age, while still only using post-2024 issues as queries.
That changes a documented scoping decision, so: **flagged, not taken.**

### Regex battery (preserved — `probe.py` is throwaway)

```python
REF = r"\[?(?:#|https://github\.com/microsoft/vscode/issues/)(\d+)"
PATTERNS = [
    ("dup_of",     re.compile(r"\b(?:duplicate|dup)\s+of\s+" + REF, re.I)),
    ("dup_colon",  re.compile(r"^\s*/?(?:duplicate|dup)\b[:\s]+" + REF, re.I | re.M)),
    ("same_as",    re.compile(r"covering the same as\s+" + REF, re.I)),
    ("tracked_in", re.compile(r"\b(?:tracked|fixed|covered)\s+(?:in|by|here)\b[^.\n]{0,40}?" + REF, re.I)),
]
```
Resolve targets with `repository.issueOrPullRequest(number:)`, **not**
`repository.issue(number:)` — the latter returns `null` for a PR number, making
"target is a PR" indistinguishable from "target was deleted".

---

## Stage 1 — Schema + GraphQL ingestion (2026-09-18)

**Goal:** get the corpus into MariaDB, idempotently and resumably.

**Rule 2 note:** there is no retrieval yet, so no recall/MRR. Stage 1's number is
the **usable labelled pair count** the ingestion actually yields — that is the
quantity every later stage's eval depends on, and it is measured below rather
than carried forward from stage 0's estimate.

**Config:** `repository.issues` connection, `orderBy CREATED_AT DESC`, page size
50, `comments(last: 20)`, `labels(first: 30)`. ~1,700 requests, ~1,700 of the
5,000/hr GraphQL point budget, sustained ~25 issues/sec. Elapsed span 125 min,
which includes the restarts from the driver bug below; the clean crawl was
roughly 60 min.

### What landed

| | |
|---|---|
| issues | **84,942** |
| comments | 208,753 |
| label rows / distinct labels | 133,799 / 627 |
| created range | 2024-01-01 00:54:06 → 2026-09-18 02:18:06 |
| on disk | issues 228MB + comments 158MB + labels 13MB ≈ **400MB** |

### Validation against stage 0

Stage 0 probed the API; stage 1 counts the same things in the database. They
agree, which is the point of doing both.

| | stage 0 (probe) | stage 1 (DB) |
|---|---|---|
| issues in window | 84,877 | 84,942 |
| `label:*duplicate` | 5,370 | 5,373 |
| `reason:duplicate` | 3,194 | 3,198 |
| overlap | 526 | 526 |
| union | 8,038 | 8,045 |

The small surplus is issues created in the hours between the probe and the fetch.

### Usable pairs — the number that matters

Running stage 0's regex battery over the ingested `comments` table:

```
duplicate-marked in DB                8,045
x canonical extractable (strong)      46.4%   (3,733)
x canonical resolves to an ingested issue  85.6%
--------------------------------------------
USABLE PAIRS                          3,194     (stage 0 estimated ~2,507)
```

Pattern split: `dup_of` 1,871 · `dup_colon` 1,784 · `tracked_in` 78 ·
`NO_MATCH` 4,312 (53.6%).

**Stage 0's estimate was conservative by 27%.** Both factors came in higher:
extraction 46.4% vs 40.4%, in-corpus 85.6% vs 77.3%. Sampling error on n=545
explains part of it; the likely remainder is that the stage 0 sample drew
deliberately from both ends of each year, and issues near a year boundary are
more likely to point at a canonical from the previous year — i.e. the
stratification that protected against bot-wording drift biased the in-corpus
rate downward. Stated as a hypothesis; not separately verified.

539 extracted canonicals point outside the corpus (pre-2024, or a PR). That is
the open decision from stage 0, still open, and `fetch.py --numbers` exists to
close it cheaply if wanted.

### Truncation — was `comments(last: 20)` the right call?

Yes, measurably. Of 84,942 issues only **436 (0.5%)** have more comments than
were fetched, and among the 8,045 duplicate-marked issues only **31 (0.4%)**.
Average thread is 2.56 comments; 14,481 issues have none at all. Paginating full
threads would have multiplied request count to recover almost nothing that stage
2 needs. `comment_count` vs `comments_fetched` makes this checkable rather than
assumed — and stage 7 will need to revisit it, since Q&A over threads cares about
the long tail that duplicate detection does not.

### Integrity

0 orphan comments · 0 issues before the date bound · 0 null bodies ·
2 "empty" titles, which turn out to be real issues titled with a single
invisible character (U+200B zero-width space, U+200E left-to-right mark).

### Driver: mysql-connector-python → PyMySQL

The first backfill died at ~250 issues with MariaDB error 1064, the message
containing a fragment of an issue body — the body was reaching the server as SQL
rather than as a bound parameter.

Two wrong diagnoses before the right one:

1. *`executemany` statement rewriting.* Plausible — mysql-connector rewrites
   multi-row INSERTs with a regex, and `ON DUPLICATE KEY UPDATE title=VALUES(title)`
   gives that regex a second `VALUES(...)` to match. Replacing `executemany` with
   a hand-built multi-row INSERT failed identically.
2. *Bad characters in the body.* Tested `%`, `%s`, quotes, backslashes, escaped
   and real newlines, emoji, plus the offending row alone and with each
   neighbour. All fine.

Bisecting on batch size found it: 1 row OK, 25 OK, 50 fails. Statement template
2,337 bytes, 600 bound parameters — so neither statement length nor
`max_allowed_packet` (16MB) was involved. Controlled comparison, identical rows
and identical statement:

| driver | result |
|---|---|
| mysql-connector-python 26.7.0 (C ext **and** pure Python) | ok=0, fail=12 |
| PyMySQL 1.2.3 | ok=12, fail=0 |

Switched to PyMySQL, which CLAUDE.md already sanctioned. Worth recording that a
driver interpolating a parameter instead of binding it is an injection shape —
here the input came from the GitHub API rather than a user, so it surfaced as a
crash, but that driver does not belong near untrusted input.

**The checkpointing paid for itself.** The crash cost zero rows; the cursor in
`fetch_state` was current and the re-run resumed from the failing page.

### Also from this stage

- `both` is a reserved word in MariaDB — cost one confusing 1064 while writing
  verification queries, which is the same error code as the driver bug and
  briefly muddied the diagnosis.
- `scripts/probe.py` deleted per CLAUDE.md. It survives in git history at
  commit `67b90a9`, and its regex battery is preserved above.
