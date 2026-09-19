# Duplicate issue detection for `microsoft/vscode`

Semantic search over **84,942 GitHub issues** that finds existing issues covering
the same bug as a new one.

The point of this project is not that it does retrieval — it is that **every
claim in it is measured**. Ground truth comes from maintainer-marked duplicates,
so retrieval quality is a number on a held-out test set, not an assertion. Three
of the seven things I tried made results *worse*, and those are in the table too.

**Live demo:** [Streamlit Community Cloud](https://share.streamlit.io) · artifacts on [HF datasets](https://huggingface.co/datasets/Musab6969/vscode-issue-rag-artifacts) · **Model:**
[`Musab6969/bge-small-vscode-dup`](https://huggingface.co/Musab6969/bge-small-vscode-dup)

---

## Result

| | recall@1 | recall@5 | recall@10 | MRR |
|---|---|---|---|---|
| random baseline | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| off-the-shelf embeddings | 0.1230 | 0.2123 | 0.2778 | 0.1627 |
| **final system** | **0.1329** | **0.2778** | **0.3393** | **0.1941** |

**+6.2 points of recall@10 over the baseline, +22% relative** (McNemar exact,
p = 0.0010 against the raw-text baseline).

Measured on **504 maintainer-marked duplicate pairs** the model never saw, with a
time filter so only issues that existed when each query was filed are searchable.

---

## The ablation table

Every row is the same 504-pair test split. **Bold is the best in each column.**

| # | retriever | recall@1 | recall@5 | recall@10 | MRR |
|---|---|---|---|---|---|
| 0 | random (floor) | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| 1 | dense `bge-small-en-v1.5` | 0.1230 | 0.2123 | 0.2778 | 0.1627 |
| 2 | dense via MariaDB HNSW index | 0.1250 | 0.2004 | 0.2679 | 0.1604 |
| 3 | BM25 (identifiers kept whole) | 0.1111 | 0.1964 | 0.2143 | 0.1423 |
| 4 | BM25 (split on punctuation) | 0.1071 | 0.1746 | 0.2024 | 0.1351 |
| 5 | BM25 (both forms) | 0.1091 | 0.1706 | 0.1984 | 0.1344 |
| 6 | hybrid dense + BM25 (RRF) | 0.1290 | 0.2222 | 0.2540 | 0.1664 |
| 7 | dense, template boilerplate stripped | 0.1230 | 0.2440 | 0.3016 | 0.1747 |
| 8 | BM25, boilerplate stripped | 0.0694 | 0.1369 | 0.1746 | 0.0993 |
| 9 | field-aware hybrid (dense-clean + BM25-raw) | **0.1369** | 0.2321 | 0.2817 | 0.1779 |
| 10 | dense, fine-tuned on duplicate pairs | 0.1349 | 0.2639 | 0.3214 | 0.1871 |
| 11 | fine-tuned dense + BM25 (RRF) | 0.1310 | 0.2381 | 0.3016 | 0.1808 |
| 12 | fine-tuned + `bge-reranker-base` rerank | 0.1032 | 0.1944 | 0.2520 | 0.1447 |
| 13 | fine-tuned + `bge-reranker-v2-m3` rerank | 0.0992 | 0.2044 | 0.2877 | 0.1479 |
| 14 | **+ hard-negative mining** | 0.1329 | **0.2778** | **0.3393** | **0.1941** |
| 15 | + title weighting (α=0.15, not shipped) | **0.1409** | 0.2817 | 0.3492 | 0.2002 |

Rows 12–13 are scored against the same candidate lists, from the fine-tuned
retriever at depth 50.

---

## What worked, what didn't

### ✅ Deleting half the input text (+2.4 recall@10)

VS Code's bug template appends a System Info table, an A/B experiment flag dump
and version headers to every report. Measured: **~47% of the 2,000 characters the
model reads** was that boilerplate. One issue's real content is *"What should the
commit message be."* — 34 characters — followed by 2,200 characters of GPU driver
strings.

Stripping it removed **53% of all text** and improved recall@10 from 0.2778 to
0.3016. The mechanism is simple: template text is near-identical across tens of
thousands of issues, so it pulled every embedding toward the same region.

### ✅ Fine-tuning a 33M model beats a 568M reranker

`bge-small-en-v1.5` (33M params) fine-tuned with `MultipleNegativesRankingLoss`
on 2,000 duplicate pairs reaches recall@10 **0.3214**.

`bge-reranker-v2-m3` (568M params, 17× larger) applied on top *lowers* it to
0.2877.

The bi-encoder learned "is this the **same bug**". The cross-encoder is a general
relevance model answering "is this document **about this topic**". Those diverge
exactly where duplicate detection is hard — the reranker promotes topically
similar issues over the actual duplicate.

### ❌ Hybrid retrieval (BM25 + reciprocal rank fusion) — no improvement

The textbook move, and it lost 2.2 points of recall@10. Rather than tune RRF, I
measured the ceiling first. At depth 50, across 504 queries:

```
found by both   120  (23.8%)
dense only       77  (15.3%)
BM25 only        15   (3.0%)   <- everything fusion could possibly gain
neither         292  (57.9%)
```

**BM25 finds 15 pairs dense misses.** A perfect fusion gains at most 3 points at
depth 50 and less at depth 10, while merging a weaker list costs dense's own
correct answers their positions. The trade is structurally bad, not badly tuned.

BM25 does work as advertised — its unique saves are literal phrase matches like
`potential listener LEAK` → `potential listener LEAK`. It just doesn't fire often
enough to pay for the dilution.

### ❌ Cross-encoder reranking — significantly worse

| reranker | Δ recall@10 | McNemar p |
|---|---|---|
| `bge-reranker-base` (278M) | −6.7 | **0.0003** |
| `bge-reranker-v2-m3` (568M) | −3.2 | 0.068 |

See above for why. The obvious fix — fine-tune the cross-encoder on the same
duplicate pairs — was not tried, and is recorded as the next experiment rather
than claimed as a result.

### ❌ The vector index was slower than brute force

MariaDB's HNSW index: **86ms and approximate**. Brute-force cosine in numpy over
the same 84,942 × 384 matrix: **32ms and exact**.

At this scale a 124MB matmul in RAM beats an index. Worse, keeping the index cost
**1h22m per bulk import** (HNSW insert time grows with the graph already built,
so the same 85k rows took 10m38s into an empty index and 1h22m into a populated
one). Dropping it made imports take **18 seconds** and changed no result.

There is also a structural problem: MariaDB only uses a `VECTOR INDEX` for a bare
`ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n`. Adding `WHERE created_at < ?`
silently disqualifies it — no error, just a full scan. Since this eval is
time-aware, **every** query needs that filter. ANN indexes and metadata filters
fight each other.

### 🔍 The two retrievers want opposite preprocessing

Stripping boilerplate helped dense (**+2.4**) and hurt BM25 (**−4.0**).

Version strings and OS builds are *rare literal tokens* — exactly what BM25's idf
weighting rewards. "Same VS Code build, same crash" is a real duplicate signal.
Dense retrieval cannot exploit repeated near-identical text and is actively
harmed by it.

Giving each retriever the representation it prefers (row 9) produced the **best
recall@1 in the project**.

---

## How the evaluation works

The evaluation is the part worth scrutinising, so here is exactly what it does.

**Ground truth.** VS Code maintainers close duplicates with a bot comment naming
the canonical issue. GitHub's structured `MarkedAsDuplicateEvent.canonical` field
is `null` throughout this repo — the bot sets the close reason without firing the
UI path that populates it — so pairs are extracted by regex over 208,753 comment
bodies.

**Extraction is deliberately precision-first.** Of 8,045 duplicate-marked issues,
2,516 yield a usable pair:

| | n | |
|---|---|---|
| no canonical named in any comment | 4,312 | the bot often says "covering the same as another one" with no number |
| canonical outside the corpus | 539 | filed pre-2024, or is a PR |
| **canonical filed *after* the duplicate** | **678** | maintainers sometimes close the older issue |
| **usable** | **2,516** | |

A further 150 candidate pairs were rejected because the only reference was a bare
`#123` somewhere in the thread; hand-inspection showed roughly half were false
("Essential cause: …", "there might be a number of related issues"). **A noisy
test set caps the measurable ceiling invisibly**, so they were dropped.

**The split is chronological**, 2,012 train / 504 test. Not random: the fine-tune
trains on duplicate pairs, and a random split would let it learn from pairs
filed *after* its own test queries.

**Every query is time-filtered.** The retriever only sees issues created before
the query issue. Without this you retrieve issues that did not exist at triage
time and recall is inflated — the RAG equivalent of look-ahead bias. It is a
required argument of the retriever signature so it cannot be forgotten.

**The harness is unit-tested.** An oracle retriever that returns the correct
answer first must score exactly 1.0; a random retriever establishes the floor at
0.0000. If the oracle were not 1.0 the metric would be broken and every number
downstream meaningless.

**Significance.** Comparisons use McNemar's exact test on discordant pairs, since
both systems answer the same 504 queries. An earlier version of this README used
a ±4-point band from the standard error of a proportion; that applies to
independent samples and was too conservative for paired A/B tests.

---

### ✅ Hard-negative mining (+1.8 recall@10) — found by a user report

A reported false positive drove this. Query #336866 (*feature request*) returned
#302623 (*bug*) — same feature area, shared phrase, not duplicates.

The cause was the training objective: `MultipleNegativesRankingLoss` draws
negatives from the rest of the batch, which are almost always about unrelated
features. Trivially easy. The model was never asked to separate "same area,
different intent."

Re-trained with negatives mined from each anchor's **own nearest neighbours**
(ranks 5–60), with a false-negative guard: if A and B are both duplicates of C
then A and B are duplicates of *each other*, so the whole chain is excluded.
Recall@10 0.3214 → **0.3393**.

### ❌ Title weighting — measured, then declined

Titles carry disproportionate signal (every rank-1 hit at baseline was a title
match), but they sit inside one concatenated embedding where a 2,000-character
body drowns a 60-character title. Scoring
`α·cos(title) + (1−α)·cos(full text)` and sweeping α:

| α | 0.0 | **0.15** | 0.3 | 0.45 | 0.6 | 0.8 | 1.0 |
|---|---|---|---|---|---|---|---|
| recall@10 | 0.3393 | **0.3492** | 0.3373 | 0.3333 | 0.3155 | 0.2937 | 0.2778 |

A single clean peak at α=0.15 — the shape of a real effect. But **p = 0.2668**
(9 gained, 4 lost), and shipping it costs a second 124MB embedding table and 60%
more latency. Measured, reported, not shipped.

One incidental result worth keeping: **title-only retrieval (α=1.0) scores
0.2778 — identical to off-the-shelf embeddings on the full text.** A 13-token
title carries as much signal as a generic embedding of the entire issue.

### Triage tools, and one that is measured

`search_duplicates`, `fetch_issue`, `suggest_labels`. The LLM orchestration loop
is code-complete but **never executed** — it needs paid API access, which was
declined for a portfolio project. Said plainly rather than implied to work.

`suggest_labels` is deliberately kNN over the same embeddings rather than an LLM
prompt, which makes it scoreable against the 627 real labels in the corpus:
**F1 0.49, and 79% of issues get at least one correct label** (content labels,
n=56). Filtering workflow labels like `*duplicate` and `info-needed` nearly
doubles precision — suggesting "this is a duplicate" as a label is circular.

## A failure you can check yourself

Query [#336866](https://github.com/microsoft/vscode/issues/336866) *"Allow
configuring the default Changes view changeset"* and the top result is
[#302623](https://github.com/microsoft/vscode/issues/302623) *"Sessions: Changes
view breaks when selecting `Last Turn's Changes`"*.

They are **not** duplicates. One is a feature request asking for a setting; the
other is a bug report about the view breaking. They share a feature area and the
distinctive phrase *"Last Turn Changes"*, which is what both the embedding and
BM25 latch onto.

This is a systematic gap, and it is measurable. Among true duplicate pairs where
both sides carry a type label, **89.3% share that type** (bug↔bug or
feature↔feature) — so intent is a strong signal that two issues are *not*
duplicates. But only 187 of 2,516 pairs (7%) have both sides labelled, and
neither issue above is labelled at all, so a metadata filter would not help here.

The root cause is the training objective. `MultipleNegativesRankingLoss` uses
random in-batch negatives — other duplicate pairs, usually about entirely
different features. Those are easy. The model was never asked to separate "same
feature area, different intent", so it did not learn to, even though the text
says *"Please add a user setting"* versus *"breaks when selecting"*.

**The fix is hard-negative mining**: train against topically-close non-duplicates
rather than random ones. Not implemented — recorded as the highest-value next
experiment.

## Why recall@10 is 0.32 and not 0.9

Three reasons, in order of size:

1. **Ground truth is maintainer-marked, so the number is a floor.** Real
   duplicates nobody ever linked count against the system as misses. There is no
   way to quantify how many.
2. **57.9% of test queries have the answer outside the top 50 of any retriever
   tried.** That is a retrieval-representation problem, not a ranking one, which
   is why fine-tuning helped and reranking did not.
3. **Duplicate detection is genuinely hard.** Reporters describe symptoms;
   maintainers title issues after causes. Three separate reports of
   `Unknown tool 'github/issue_read'` are duplicates of one titled
   *"Problems panel showing problems from Copilot configuration files"* — no
   shared vocabulary at all.

A system reporting 0.9 on this task is almost certainly evaluating on something
easier, such as issues with near-identical titles. In this corpus those exist and
are trivially found — every rank-1 hit at the baseline was a case like
*"Never ending generation."* → *"Never ending generation."*, filed minutes apart
with the same typo.

---

## Architecture

```
GitHub GraphQL ──► MariaDB ──► embed (Colab GPU) ──► fine-tune ──► evaluate
                                                                      │
                                                        scripts/export.py
                                                                      ▼
                                                   artifacts/ ──► HF dataset ──► Streamlit Cloud
                                                   (4 files, no secrets)
```

**Batch and serving are strictly separated.** The Space reads four files into RAM
and answers queries — no database, no GitHub API, no credentials. Nothing
external can break the demo.

| | |
|---|---|
| corpus | 84,942 issues, created ≥ 2024-01-01 |
| storage | MariaDB 12.3 (`VECTOR` type available, deliberately unused — see above) |
| embeddings | 384 dims float32, 124MB, brute-force cosine in numpy |
| retrieval latency | ~35ms local, ~380ms cold on a free Space |

---

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install --index-url https://download.pytorch.org/whl/cpu torch

cp .env.example .env          # GITHUB_TOKEN + DB credentials
python -m src.db --init
python -m src.fetch           # ~85k issues, resumable
python -m src.testset         # build the frozen test set
python -m src.embed --export-texts colab/texts.jsonl.gz --clean
#   encode on a GPU, then:
python -m src.embed --import-vectors shards/ --clean
python -m src.retrieve        # the ablation numbers
streamlit run app.py
```

`NOTES.md` is the full experiment log — every number above, the configuration
that produced it, and what was learned from the ones that failed.

---

## Limitations

- **One repository.** The extraction regexes encode `microsoft/vscode`'s triage
  conventions. Generalising means re-deriving them per repo.
- **Frozen snapshot.** The corpus is fixed at 2026-09-18. `fetch.py --since`
  supports incremental refresh, keyed on `updatedAt` rather than `createdAt`
  because an issue filed in 2024 can be marked duplicate in 2026.
- **n = 504.** Individual steps in the pipeline are not separable at this sample
  size — boilerplate stripping alone is p = 0.073 and the fine-tune alone is
  p = 0.19. Only the end-to-end improvement clears significance (p = 0.0086).
- **The reranker was not fine-tuned.** The most likely way to make stage 5 work,
  and it was not tried.
