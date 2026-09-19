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

> **Corrected in stage 2.** This 3,194 counts pairs the *eval cannot use*. 678 of
> them (8.4%) have a canonical created **after** the duplicate — maintainers do
> sometimes close the older issue as a duplicate of the newer one. A time-aware
> retriever can never return those, so the real usable count is **2,516**, and
> stage 0's estimate of 2,507 was almost exactly right. The "27% conservative"
> claim below was comparing the wrong quantity.

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

---

## Stage 2 — Eval harness + test-set extraction (2026-09-18)

**Written by Claude at the user's explicit request**, overriding CLAUDE.md rule 3
("I write eval.py myself"). Recorded because the rule still stands for
`src/fusion.py`.

**Files:** `src/testset.py` (extraction → frozen `testset.json`), `src/eval.py`
(metrics), `db.issue_text()` (shared text composition).

### Why extraction and evaluation are separate files

Extraction is regex over 208,753 comments and produces a **frozen, committed
artifact**. Evaluation reads it and runs many times. If extraction ran inside
every eval, editing the regex battery would silently move the benchmark and the
stage-to-stage numbers in this file would stop being comparable. The whole value
of an ablation table is that only one thing changes at a time.

### Extraction funnel (8,045 duplicate-marked issues)

| stage | n | share |
|---|---|---|
| no canonical in comment text | 4,312 | 53.6% |
| canonical not in corpus (pre-2024 or a PR) | 539 | 6.7% |
| **canonical newer than the query** | **678** | **8.4%** |
| **usable** | **2,516** | **31.3%** |

Pattern hits: `dup_of` 1,871 · `dup_colon` 1,784 · `tracked_in` 78.

**The 8.4% time-ordering drop is new information and it corrects stage 1.**
Maintainers sometimes close the *older* issue as a duplicate of the newer one.
Those pairs are unusable for a time-aware eval by construction: no retriever
restricted to issues predating the query could ever return the answer. Keeping
them would have depressed every future recall number by ~8% for a reason that
has nothing to do with retrieval quality — and, worse, it would have looked like
a model problem.

126 pairs have a canonical that is itself marked duplicate (A→B→C chains). Left
intact: B is a real corpus issue and retrieving it for query A is exactly what a
maintainer labelled correct. Recorded because it is a defensible-either-way call.

### Split

**Chronological, 80/20 — train 2,012, test 504.** Not random, for two reasons:

1. Stage 5b fine-tunes on duplicate pairs. A random split would let it train on
   pairs that postdate its own test queries — leakage that would inflate 5b and
   nothing else, making the ablation table lie about exactly the stage it was
   built to measure.
2. It mirrors deployment: learn from history, answer new issues.

### Harness self-check — stage 2's number

There is no retriever yet, so the number that matters is whether the metric is
correct. Two synthetic retrievers bracket it:

| retriever | recall@1 | recall@5 | recall@10 | MRR |
|---|---|---|---|---|
| **oracle** (returns the answer first) | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **random** (sampled from issues predating the query) | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

The oracle is a unit test, not a baseline: if it were not exactly 1.0 the harness
would be broken and every later number noise. The random floor is 0.0000 because
10 draws from a ~50k-issue candidate pool gives ~0.1 expected hits across 504
queries — so **any** real signal in stage 3 will be visible.

### Statistical precision — read this before trusting small deltas

With **n = 504** test pairs, a measured recall of ~0.30 has a standard error of
about **±2.0 points**, so a 95% CI is roughly ±4 points. **Differences smaller
than ~4 points between stages are not significant.** Worth stating now, before
there is any temptation to narrate a 1-point improvement as a win. If a later
stage needs finer resolution, the lever is a larger test fraction, not a better
story.

### Retriever contract

    retriever(query_text: str, before_date: datetime, n: int) -> list[int]

`before_date` is enforced, not advisory. A retriever ignoring it scores well here
and is worthless in production, because it answers with issues that did not exist
when the question was asked. `eval.py` additionally strips the query issue from
any result list, so a bug of that shape shows up as a bad score rather than a
suspiciously good one.

MRR is truncated at `max(ks)` — a canonical ranked below the retrieved window
contributes 0, not an unknown `1/rank`. Cross-stage MRR comparisons are only
valid at the same depth.

`db.issue_text(title, body)` lives in the data layer so eval and stage 3's
`embed.py` cannot drift apart. If the query side and document side composed text
differently, the numbers would be measuring that discrepancy as much as the model.

---

## Stage 3 — Dense baseline (2026-09-19)

**Config:** `BAAI/bge-small-en-v1.5`, 384 dims, L2-normalised, **no** `query:`/
`passage:` prefix (retrieval is symmetric — both sides are issue text). Text is
`title + "\n\n" + body` truncated to 2,000 chars, which is past the model's
512-token window. Encoded on a Colab T4 in ~2 min; scored on the 504-pair test
split.

### The number

| retriever | recall@1 | recall@5 | recall@10 | MRR | p50 latency |
|---|---|---|---|---|---|
| **dense brute-force (numpy)** | **0.1230** | **0.2123** | **0.2778** | **0.1627** | 32.1 ms |
| dense HNSW (MariaDB index) | 0.1250 | 0.2004 | 0.2679 | 0.1604 | 85.7 ms |
| *random floor (stage 2)* | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 1.1 ms |

**Brute-force numpy is the baseline of record: recall@10 = 0.2778.** The correct
duplicate reaches the top 10 for roughly one query in four, and the top 1 for one
in eight. As predicted, that is bad — and it is the number every later stage is
measured against.

### Brute-force vs HNSW

The two are **statistically indistinguishable on accuracy**: the largest gap
(recall@10, 0.2778 vs 0.2679) is 1.0 point, well inside the ±4-point significance
band recorded in stage 2. Do not read the HNSW recall@1 edge as a win; it is two
pairs.

The real difference is latency, and it runs *against* the index: 32ms vs 86ms.
Three reasons, all specific to this scale:

1. Brute force is one 84,942 × 384 matmul in-process — ~124MB, trivially cached.
2. The HNSW path costs two SQL round trips per query (vector search, then the
   date filter) plus protocol overhead.
3. It over-fetches 10× candidates to survive post-filtering, so it does more
   index work than the 10 results need.

**The index is not justified at this corpus size, and the README should say so
plainly.** It earns its place when the matrix stops fitting in RAM. Keeping both
paths is what makes that a measured claim rather than an assumption.

`EXPLAIN` confirms the index is genuinely used on the bare query, not silently
skipped:

```
(1, 'SIMPLE', 'embeddings', 'index', None, 'vec', '1538', None, '10', '')
                             ^^^^^^^        ^^^^^
```

### Time filtering defeats the vector index — the structural finding

MariaDB uses a `VECTOR INDEX` only for a bare
`ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n`. Adding `WHERE created_at < ?`
disqualifies it, with no error and no warning — just a silent full scan.

Since this eval is time-aware by construction, **every** query needs that filter.
The workaround (`MariaDBVectorRetriever`) over-fetches `n × 10` neighbours from
the bare indexed query and filters by date afterwards, which is approximate a
second time: if enough near neighbours postdate the query, fewer than `n`
survive. Brute-force numpy has no such problem — vectors are held in `created_at`
order, so the time filter is an array slice.

### Ingest cost

Importing 84,942 vectors took **10m38s**, of which only 4.7s was Python CPU. The
rest is MariaDB building the HNSW graph incrementally, one insert at a time. Table
is 226MB: ~124MB of vectors plus ~100MB of index. Worth knowing before stage 5b,
which requires a second full encode and import.

### Examples

**What it gets right — near-identical text.** All rank-1 hits look like this:

| query | canonical | rank |
|---|---|---|
| #304776 "Never ending generation." | #304775 "Never ending generation." | 1 |
| #304812 "remove the code usggestion until i say to show" | #304811 *(identical title)* | 1 |
| #304858 "High CPU usage when multiple extensions register many chatAgent…" | #304857 *(identical)* | 1 |

These are duplicates filed minutes apart with the same title, including the same
typo. Embeddings are not doing much work here — string equality would find them.

**What it misses — same bug, different vocabulary.** Three separate issues all
point at canonical #302880 and none retrieved it in the top 10:

| query | canonical |
|---|---|
| #305022 "Custom agent front matter reports unknown tool for github/issue_read" | #302880 "Problems panel showing problems from Copilot configuration files" |
| #305137 `Unknown tool "problems" with v1.113.0` | #302880 |
| #305190 `Unknown tool 'github/issue_read' warning in Copilot Chat` | #302880 |

The reporter describes a symptom (*"unknown tool"*); the maintainer titled the
canonical after the cause (*"Problems panel…"*). No shared title vocabulary, and
bge-small does not bridge it.

### Stage 4 prediction — qualified, because the evidence is mixed

Checked the bodies directly rather than assuming:

| token | in query #305190 | in canonical #302880 |
|---|---|---|
| `unknown tool` | ✅ | ✅ |
| `issue_read` | ✅ | ❌ |
| `problems` | ❌ | ✅ |

So BM25 **does** have a real hook — `unknown tool` appears in both bodies, a
lexical bridge the embedding failed to exploit. But the *most distinctive* token,
`issue_read`, is absent from the canonical entirely, so the obvious rare-term
match is not available.

**Prediction: stage 4 helps on this cluster, but less than the "BM25 catches
identifiers" story suggests.** Recorded now, before running it, so the result can
falsify it rather than be narrated after the fact.

---

## Stage 4 — BM25 + RRF hybrid (2026-09-19)

**Config:** RRF k=60 (user-written `src/fusion.py`), each retriever contributes
its top 50 before fusion, scored on the same 504-pair test split. BM25 indexes
the same 2,000-char text the dense side embeds, so the comparison isolates the
method rather than how much text each side got.

### The number

| retriever | recall@1 | recall@5 | recall@10 | MRR | p50 |
|---|---|---|---|---|---|
| **dense only** | 0.1230 | 0.2123 | **0.2778** | 0.1627 | 35ms |
| bm25 (atomic) | 0.1111 | 0.1964 | 0.2143 | 0.1423 | 79ms |
| bm25 (words) | 0.1071 | 0.1746 | 0.2024 | 0.1351 | 120ms |
| bm25 (both) | 0.1091 | 0.1706 | 0.1984 | 0.1344 | 153ms |
| hybrid (atomic) | 0.1290 | 0.2222 | 0.2540 | 0.1664 | 147ms |
| hybrid (words) | 0.1250 | 0.2202 | 0.2560 | 0.1641 | 195ms |
| hybrid (both) | 0.1290 | 0.2262 | 0.2560 | 0.1660 | 218ms |

**Hybrid does not beat dense. It loses 2.2 points of recall@10** (0.2778 →
0.2560) while gaining ~1.4 on recall@1 and recall@5. Every one of those gaps is
inside the ±4-point band stage 2 established, so the honest statement is:
**stage 4 produced no significant improvement in either direction.**

Dense-only remains the retriever of record at recall@10 = 0.2778.

### Why — the ceiling diagnostic

Rather than tune RRF and hope, measured how much unique signal BM25 has at all.
At depth 50 on the same 504 queries:

| | n | share |
|---|---|---|
| found by both | 120 | 23.8% |
| dense only | 77 | 15.3% |
| **BM25 only** | **15** | **3.0%** |
| neither | 292 | 57.9% |

```
dense recall@50   0.3909
bm25  recall@50   0.2679
UNION recall@50   0.4206   <- hard ceiling for ANY fusion method
```

**BM25 finds 15 pairs dense misses.** A *perfect* fusion — one that always
promoted the right answer — could gain 3.0 points at depth 50, and much less by
depth 10. Meanwhile merging a weaker list costs dense's own correct answers
their positions. The measured −2.2 is that trade, and it is not a tuning
failure: the ceiling is structural.

This is why the diagnostic was worth running before touching k or depth. Tuning
a knob whose maximum payoff is 3 points is not where the effort belongs.

### Prediction check — I was directionally right and still too optimistic

Stage 3 recorded, before running this: *"stage 4 helps on this cluster, but less
than the 'BM25 catches identifiers' story suggests."*

Half right. BM25 does behave exactly as theory says — its 15 unique saves are
literal phrase matches:

> #307563 `[Unhandled Error] potential listener LEAK detected, popula…`
> → #304828 `[042/d9f] potential listener LEAK in chatTerminalToolProgr…`

> #318389 `Agent picker doesn't persist selection on an existing sess…`
> → #318388 `switches out of custom agent mode after request`

But "helps less than expected" was still too generous. It does not help at
recall@10 at all. **Recording the prediction beforehand is what makes this a
result rather than a rationalisation.**

### Tokenizer comparison

Three tokenizations of `Unknown tool github/issue_read with v1.113.0`:

| tokenizer | produces | bm25 recall@10 | vocab |
|---|---|---|---|
| `atomic` (whitespace only) | `github/issue_read`, `v1.113.0` | **0.2143** | 363k |
| `words` (alphanumeric runs) | `github`,`issue`,`read`,`v1`,`113`,`0` | 0.2024 | 310k |
| `both` (whole + parts) | both of the above | 0.1984 | 586k |

`atomic` is best for BM25 alone, by 1.2–1.6 points — directionally supporting
"keep identifiers whole", though inside the noise band. Splitting identifiers
apart produces common tokens (`issue`, `read`, `vs`) that dilute the signal, and
`both` is worst because it does that *and* inflates the vocabulary 61%.

Once fused, the tokenizer stops mattering (0.2540 / 0.2560 / 0.2560) — dense
dominates the merged ranking either way.

### The finding that should shape stage 5

**57.9% of queries have the canonical in neither retriever's top 50.**

A cross-encoder reranker reorders candidates; it cannot invent one. That caps
stage 5's achievable recall@10 at 0.4206 no matter how good the reranker is, and
realistically well below it. **The lever for that 57.9% is better first-stage
retrieval — the stage 5b fine-tune — not reranking.** Stage 5 should therefore
be judged on MRR and recall@1 (reordering what was found) rather than on
recall@10.

### BM25 implementation note

`rank_bm25` was too slow to use: `BM25Okapi.get_scores` recomputes the length
normalisation across all 84,942 documents for every query token, and an issue
query is ~290 tokens — **2.7s per query**, i.e. 22 min per eval and over 2 hours
for the seven configurations here.

Everything except the idf lookup depends only on the document, so `BM25Index`
precomputes the per-(document, term) weight once into an inverted index and a
query becomes a scatter-add over postings lists.

Verified against the library rather than assumed: same Okapi formula
(k1=1.5, b=0.75, epsilon=0.25), **top-10 identical on every test query, max
score difference 8.8e-05** (float32 vs float64), **147× faster** (1346ms → 9.2ms
on a 4k-doc corpus). `rank_bm25` stays in `requirements.txt` as the reference
implementation the tests validate against.

Build cost on the full corpus: 91–159s per tokenizer, ~2.7GB peak RSS.

---

## Stage 6a — Strip template boilerplate (2026-09-19)

Pulled forward from stage 6 because everything downstream trains or scores on
this text. Same model, same corpus, same 504-pair test split — **only the input
text differs.**

### What was removed

vscode's bug template appends a `System Info` table, an `A/B Experiments` flag
dump and version headers to every report. Measured over the whole corpus:

| | before | after |
|---|---|---|
| mean chars in the 2,000-char window | 1,327 | 623 |
| median | 1,613 | 401 |
| total text volume | 112.7M | 52.9M (**−53.0%**) |

28.6% of issues were unchanged (hand-written, no template). 0.5% reduced to
title only — inspected, and those bodies genuinely were all boilerplate.

The extreme case: #307631's real content is *"What should the commit message
be."* — 34 characters — followed by 2,200 characters of GPU driver strings.

### The number

| retriever | recall@1 | recall@5 | recall@10 | MRR |
|---|---|---|---|---|
| dense (raw text) | 0.1230 | 0.2123 | 0.2778 | 0.1627 |
| **dense (boilerplate stripped)** | 0.1230 | **0.2440** | **0.3016** | **0.1747** |

**+2.4 points recall@10, +3.2 recall@5, +1.2 MRR, recall@1 unchanged** — from
deleting text, with no new model and no training.

### Significance — and a correction to stage 2's rule

Stage 2 recorded "differences smaller than ~4 points are not significant." **That
rule is wrong for this comparison and needs qualifying.** It was derived from the
standard error of a single proportion at n=504, which applies when comparing two
*independent* samples. Here both systems answer the *same* queries, so the right
test is McNemar's on the discordant pairs:

```
both correct        127
both wrong          339
raw only   (lost)    13
clean only (gained)  25
net +12 queries, McNemar exact two-sided p = 0.0730
```

**p = 0.073 — suggestive, not significant at 0.05.** Much closer than the ±4
independent-sample band implied, but it does not clear the bar.

Going forward: use the ±4-point band for comparing against an *external*
baseline, and McNemar's for any A/B where the same test set is scored twice.
Nearly every comparison in this project is the paired kind.

### Kept anyway, and why that is not cherry-picking

The change is adopted despite p=0.073:

1. It is better or equal on **all four metrics**, never worse.
2. The direction is consistent across recall@5, recall@10 and MRR, which a noise
   explanation has to account for.
3. It is a **prerequisite for stage 5b** regardless of its own effect size —
   fine-tuning on text that is half GPU driver strings would teach the model to
   read them.
4. It halves storage and encode cost.

If the only argument were the recall delta, p=0.073 would not justify the claim.
The honest framing for the README is "a consistent improvement that does not
reach significance on 504 queries," not "boilerplate stripping improves recall."

### Cost note

Importing the second set of 84,942 vectors took **1h22m**, versus 10m38s for the
first. The HNSW index now holds 170k vectors and every insert walks a bigger
graph. Worth planning for: stage 5b adds a third full set, which will be slower
again. If re-encoding becomes routine, drop the vector index before a bulk load
and rebuild it after.
